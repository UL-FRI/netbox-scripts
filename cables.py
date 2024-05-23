import decimal
import re

from django.contrib.contenttypes.models import ContentType

from dcim.models import Cable, CableTermination, Device, DeviceType, Interface, Location, FrontPort
from tenancy.models import Tenant
from extras.scripts import *

class ConnectPanelsScript(Script):
    class Meta:
        name = "Connect panels"
        description = "Connect front ports on patch panels to access switches"
        scheduling_enabled = False

    locations = MultiObjectVar(model=Location,
            description='Connect all ports in these locations')
    exclude_ports = MultiObjectVar(model=Device,
            query_params={'location_id': '$locations'}, required=False,
            description='Exclude these ports')
    tenant = ObjectVar(model=Tenant, required=False,
            description='Set this tenant for ports')

    def run(self, data, commit):
        port_regex = r'^([0-9]+)\.([0-9]+)$'
        port_type = DeviceType.objects.get(model='RJ45 access port')
        fri_it = Tenant.objects.get(name='FRI IT')

        for port in Device.objects \
                .filter(location__in=data['locations'], device_type=port_type) \
                .exclude(id__in=data['exclude_ports']):
            if m := re.match(port_regex, port.name):
                # set tenant on access port
                if tenant := data['tenant']:
                    port.tenant = tenant
                    port.full_clean()
                    port.save()
                    self.log_info(f'set tenant on {port} to {tenant}')

                # get the panel and adjacent switch above or below
                panel = Device.objects.get(name=f'panel-{m[1]}')
                switch = panel.rack.devices.get(
                        role__name__iexact='switch',
                        position__in=[panel.position+1, panel.position-1])

                # get front port and switch interface
                port_num = int(m[2])
                iface_num = port_num*2 if panel.position < switch.position else port_num*2-1

                fport = panel.frontports.get(name=port_num)
                iface = switch.interfaces.get(name__regex=f'.*[^0-9]{iface_num}$', member_interfaces=None)

                # connect if no cable exists at either end
                if fport.link:
                    self.log_info(f'{panel}:{fport} already connected')
                elif iface.link:
                    self.log_info(f'{switch}:{iface} already connected')
                else:
                    cable = Cable.objects.create(status='planned',
                        tenant=fri_it, type='cat6a', color='9e9e9e',
                        length=decimal.Decimal(15.00), length_unit='cm')
                    CableTermination.objects.create(
                        cable=cable, cable_end='A',
                        termination_id=fport.id,
                        termination_type=ContentType.objects.get_for_model(FrontPort))
                    CableTermination.objects.create(
                        cable=cable, cable_end='B',
                        termination_id=iface.id,
                        termination_type=ContentType.objects.get_for_model(Interface))
                    cable.full_clean()
                    cable.save()
                    self.log_info(f'connected {panel}:{fport} to {switch}:{iface}')

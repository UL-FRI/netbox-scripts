from dcim.models import Device, Interface
from ipam.models import FHRPGroup, FHRPGroupAssignment, IPAddress, Prefix, Role, VLAN, VLANGroup, VRF
from tenancy.models import Tenant
from extras.scripts import *
import netaddr

class CreateVLANScript(Script):
    class Meta:
        name = 'Create VLAN'
        description = 'Create and configure a new VLAN on exit switches'
        scheduling_enabled = False

    vlan_name = StringVar(label='VLAN name', regex='[a-z-]', max_length=15)
    vlan_id = IntegerVar(label='VLAN ID', min_value=2, max_value=4094)
    tenant = ObjectVar(model=Tenant)
    net4 = IPNetworkVar(label='IPv4 network', required=False,
            description='IPv4 network for this VLAN')
    net6 = IPNetworkVar(label='IPv6 network', required=False,
            description='IPv6 network for this VLAN')
    firewall = BooleanVar(label='Firewall?', default=True,
            description='Use a separate VRF behind the firewall or the outside VRF')

    def run(self, data, commit):
        tenant = data['tenant']
        vlan_id = data['vlan_id']
        vlan_name = data['vlan_name']
        net4 = data.get('net4')
        net6 = data.get('net6')
        firewall = data['firewall']

        fri_it = Tenant.objects.get(name='FRI IT')

        # get or create the VRF for this VLAN
        if firewall:
            vrf, new = VRF.objects.get_or_create(name=vlan_name)
            vrf.tenant = tenant
        else:
            vrf, new = VRF.objects.get_or_create(name='outside')
            vrf.tenant = None
        self.log_info(f'{"created" if new else "got"} VRF {vrf}')
        vrf.full_clean()
        vrf.save()

        # get or create the VLAN
        vlan, new = VLAN.objects.get_or_create(vid=vlan_id)
        vlan.tenant = tenant
        vlan.name = vlan_name
        vlan.group = VLANGroup.objects.get(name='new-net')
        vlan.full_clean()
        vlan.save()
        self.log_info(f'{"created" if new else "got"} VLAN {vlan}')

        # get or create the FHRP group for virtual router IPs
        fhrp_group, new = FHRPGroup.objects.get_or_create(name=vlan_name, group_id=vlan_id, protocol='other')
        self.log_info(f'{"created" if new else "got"} FHRP group {fhrp_group}')

        # get or create prefixes
        prefixes = []
        for net in [net4, net6]:
            if net:
                prefix, new = Prefix.objects.get_or_create(prefix=net)
                self.log_info(f'{"created" if new else "got"} prefix {prefix.prefix}')
                prefix.tenant = tenant
                prefix.vrf = vrf
                prefix.vlan = vlan
                prefix.tenant = tenant
                prefix.role = None
                prefix.full_clean()
                prefix.save()
                prefixes += [prefix]

                vip, new = IPAddress.objects.get_or_create(address=netaddr.IPNetwork((prefix.prefix.first+1, prefix.prefix.prefixlen)))
                self.log_info(f'{"created" if new else "got"} vip {vip}')
                vip.tenant = fri_it
                vip.vrf = vrf
                vip.save()
                fhrp_group.ip_addresses.add(vip)

        fhrp_group.full_clean()
        fhrp_group.save()

        # create or update bridge child interface on each exit
        exits = Device.objects.filter(role__slug='switch', name__startswith='exit-').order_by('name')
        for index, switch in enumerate(exits):
            bridge = switch.interfaces.get(name='bridge')
            child, new = bridge.child_interfaces.get_or_create(device=switch, name=f'bridge.{vlan_id}')
            child.type = 'virtual'
            child.vrf = vrf
            child.mode = 'access'
            child.untagged_vlan = vlan

            fhrp_group_assignment, new = child.fhrp_group_assignments.get_or_create(group_id=fhrp_group.id, priority=0)
            self.log_info(f'{"created" if new else "got"} fhrp_group_assignment {fhrp_group_assignment}')

            child.full_clean()
            child.save()
            self.log_info(f'{"created" if new else "got"} interface {child} on {switch}')

            for prefix in prefixes:
                network = prefix.prefix
                addr_switch = netaddr.IPNetwork((network.first+2+index, network.prefixlen))
                address, new = child.ip_addresses.get_or_create(address=addr_switch)
                self.log_info(f'{"created" if new else "got"} address {address}')
                address.vrf = vrf
                address.tenant = fri_it
                address.role = ''
                address.full_clean()
                address.save()

        self.log_success(f'wee!')


class SetVLANScript(Script):
    class Meta:
        name = 'Set VLAN'
        description = 'Set tagged and untagged VLANs on access switch ports'
        fieldsets = (
            ('Ports', ('access_ports', 'switch', 'switch_ports')),
            ('Settings', ('vlans', 'enable'))
        )
        scheduling_enabled = False

    access_ports = MultiObjectVar(model=Device, required=False,
            query_params={'device_type': 'rj45-access-port'},
            description='These ports will be traced to corresponding switch ports')
    switch = ObjectVar(model=Device, required=False,
            query_params={'role': 'switch'},
            description='Limit selection to this switch')
    switch_ports = MultiObjectVar(model=Interface, required=False,
            query_params={'device_id': '$switch'},
            description='Select switch ports directly')
    vlans = MultiObjectVar(model=VLAN, required=False,
            label='VLANs',
            description='Select multiple VLANs to put selected ports into tagged mode')
    enable = BooleanVar(label='Enable ports', default=True)

    def run(self, data, commit):
        all_ports = list(data['switch_ports'])
        modified_switches = set()

        # trace doesn’t work for rear ports for some reason, so do it manually
        # assumes this layout (f=front port, r=rear port, i=interface, ---=cable):
        # 1f:012.23:r1 --- 23r:panel-012:f23 --- 46i:sw-xyzzy
        for device in data['access_ports']:
            rearport = device.rearports.first()
            panel_rearport = rearport.link_peers[0]
            panel_frontport = panel_rearport.frontports.first()
            all_ports += panel_frontport.link_peers

        for port in all_ports:
            port.enabled = data['enable']
            match len(data['vlans']):
                case 0:
                    port.mode = 'access'
                    port.tagged_vlans.clear()
                    port.untagged_vlan = None
                case 1:
                    port.mode = 'access'
                    port.tagged_vlans.clear()
                    port.untagged_vlan = data['vlans'][0]
                case _:
                    port.mode = 'tagged'
                    port.tagged_vlans.set(data['vlans'])
                    port.untagged_vlan = None
            port.full_clean()
            port.save()
            modified_switches.add(port.device.name)
            self.log_info(f'{port.device.name} {port} is {port.mode} for {",".join(str(vlan.vid) for vlan in data["vlans"])}')

        self.log_success(f'modified switches {",".join(sorted(modified_switches))}')

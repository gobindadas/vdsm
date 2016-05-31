#
# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import

from copy import deepcopy

import six

import vdsm.config
from vdsm.network import kernelconfig

from testlib import VdsmTestCase

# PY3 does not support m2crypto, therefore force standard python ssl for tests
import sys
if sys.version_info >= (3, 0):
    vdsm.config.config.set('vars', 'ssl_implementation', 'ssl')

from functional.utils import getProxy, SUCCESS

CAPS_INFO = 2
USING_UNIFIED_PERSISTENCE = (
    vdsm.config.config.get('vars', 'net_persistence') == 'unified')

NOCHK = {'connectivityCheck': False}


class NetFuncTestCase(VdsmTestCase):

    def __init__(self, *args, **kwargs):
        VdsmTestCase.__init__(self, *args, **kwargs)
        self.vdsm_proxy = getProxy()

    def update_netinfo(self):
        self.netinfo = self.vdsm_proxy.netinfo

    def update_running_config(self):
        self.running_config = self.vdsm_proxy.config

    @property
    def setupNetworks(self):
        return SetupNetworks(self.vdsm_proxy, self._setup_networks_post_hook())

    def _setup_networks_post_hook(self):
        def assert_kernel_vs_running():
            # Refresh caps and running config data
            self.update_netinfo()
            self.update_running_config()

            if USING_UNIFIED_PERSISTENCE:
                self.assert_kernel_vs_running_config()
        return assert_kernel_vs_running

    def assertNetwork(self, netname, netattrs):
        """
        Aggregates multiple network checks to ease usage.
        The checks are between the requested setup (input) and current reported
        state (caps).
        """
        self.assertNetworkExists(netname)

        bridged = netattrs.get('bridged', True)
        if bridged:
            self.assertNetworkBridged(netname)
        else:
            self.assertNetworkBridgeless(netname)

        self.assertHostQos(netname, netattrs)

        self.assertSouthboundIface(netname, netattrs)
        self.assertVlan(netattrs)

    def assertHostQos(self, netname, netattrs):
        network_caps = self.netinfo.networks[netname]
        if 'hostQos' in netattrs:
            qos_caps = _normalize_qos_config(network_caps['hostQos'])
            self.assertEqual(netattrs['hostQos'], qos_caps)

    def assertNetworkExists(self, netname):
        self.assertIn(netname, self.netinfo.networks)

    def assertNetworkBridged(self, netname):
        network_caps = self.netinfo.networks[netname]
        self.assertTrue(network_caps['bridged'])
        self.assertIn(netname, self.netinfo.bridges)

    def assertNetworkBridgeless(self, netname):
        network_caps = self.netinfo.networks[netname]
        self.assertFalse(network_caps['bridged'])
        self.assertNotIn(netname, self.netinfo.bridges)

    def assertSouthboundIface(self, netname, netattrs):
        nic = netattrs.get('nic')
        bond = netattrs.get('bonding')
        vlan = netattrs.get('vlan')
        bridged = netattrs.get('bridged', True)

        if bridged:
            iface = netname
        elif vlan is not None:
            iface = '{}.{}'.format(nic or bond, vlan)
        else:
            iface = nic or bond

        network_caps = self.netinfo.networks[netname]
        self.assertEquals(iface, network_caps['iface'])

    def assertVlan(self, netattrs):
        vlan = netattrs.get('vlan')
        if vlan is None:
            return

        nic = netattrs.get('nic')
        bond = netattrs.get('bonding')
        iface = '{}.{}'.format(nic or bond, vlan)

        self.assertIn(iface, self.netinfo.vlans)
        vlan_caps = self.netinfo.vlans[iface]
        self.assertTrue(isinstance(vlan_caps['vlanid'], int))
        self.assertEqual(int(vlan), vlan_caps['vlanid'])

    def assertBridgeOpts(self, netname, netattrs):
        custom_attrs = netattrs.get('custom', {})
        if 'bridge_opts' in custom_attrs:
            bridge_caps = self.netinfo.bridges[netname]
            req_bridge_opts = (opt.split('=', 1) for opt in
                               custom_attrs['bridge_opts'].split(' '))
            bridge_opts_caps = bridge_caps['opts']
            for br_opt, br_val in six.iteritems(req_bridge_opts):
                self.assertEqual(br_val, bridge_opts_caps[br_opt])

    # FIXME: Redundant because we have NetworkExists + kernel_vs_running_config
    def assertNetworkExistsInRunning(self, netname, netattrs):
        if not USING_UNIFIED_PERSISTENCE:
            return
        self.update_running_config()
        netsconf = self.running_config.networks

        self.assertIn(netname, netsconf)
        netconf = netsconf[netname]

        bridged = netattrs.get('bridged')
        self.assertEqual(bridged, netconf.get('bridged'))

    def assertNoNetwork(self, netname):
        self.assertNoNetworkExists(netname)
        self.assertNoBridgeExists(netname)
        self.assertNoNetworkExistsInRunning(netname)

    def assertNoNetworkExists(self, net):
        self.assertNotIn(net, self.netinfo.networks)

    def assertNoBridgeExists(self, bridge):
        self.assertNotIn(bridge, self.netinfo.bridges)

    def assertNoVlan(self, southbound_port, tag):
        vlan_name = '{}.{}'.format(southbound_port, tag)
        self.assertNotIn(vlan_name, self.netinfo.vlans)

    def assertNoNetworkExistsInRunning(self, net):
        if not USING_UNIFIED_PERSISTENCE:
            return

        self.update_running_config()
        self.assertNotIn(net, self.running_config.networks)

    def assert_kernel_vs_running_config(self):
        """
        This is a special test, that checks setup integrity through
        non vdsm api data.
        The networking configuration relies on a semi-persistent running
        configuration files, describing the requested configuration.
        This configuration is checked against the actual caps report.
        """

        running_config = kernelconfig.normalize(self.running_config)
        running_config = running_config.as_unicode()

        netinfo = _normalize_caps(self.netinfo)
        kernel_config = kernelconfig.KernelConfig(netinfo)
        kernel_config = kernel_config.as_unicode()

        # Do not use KernelConfig.__eq__ to get a better exception if something
        # breaks.
        self.assertEqual(running_config['networks'], kernel_config['networks'])
        self.assertEqual(running_config['bonds'], kernel_config['bonds'])


class SetupNetworksError(Exception):
    pass


class SetupNetworks(object):

    def __init__(self, vdsm_proxy, post_setup_hook):
        self.vdsm_proxy = vdsm_proxy
        self.post_setup_hook = post_setup_hook

    def __call__(self, networks, bonds, options):
        self.setup_networks = networks

        status, msg = self.vdsm_proxy.setupNetworks(networks, bonds, options)
        if status != SUCCESS:
            raise SetupNetworksError(msg)

        try:
            self.post_setup_hook()
        except:
            # Ignore cleanup failure, make sure to re-raise original exception.
            self._cleanup()
            raise

        return self

    def __enter__(self):
        pass

    def __exit__(self, type, value, traceback):
        status, msg = self._cleanup()
        if type is None and status != SUCCESS:
            raise SetupNetworksError(msg)

    def _cleanup(self):
        networks_caps = self.vdsm_proxy.netinfo.networks
        NETSETUP = {net: {'remove': True}
                    for net in self.setup_networks if net in networks_caps}
        status, msg = self.vdsm_proxy.setupNetworks(NETSETUP, {}, NOCHK)
        return status, msg


def _normalize_caps(netinfo_from_caps):
    """
    Normalize network caps to allow kernel vs running config comparison.

    The netinfo object used by the tests is created from the network caps data.
    To allow the kernel vs running comparison, it is required to revert the
    caps data compatibility conversions (required by the oVirt Engine).
    """
    netinfo = deepcopy(netinfo_from_caps)
    # TODO: When production code drops compatibility normalization, remove it.
    for dev in six.itervalues(netinfo.networks):
        dev['mtu'] = int(dev['mtu'])

    return netinfo


def _normalize_qos_config(qos):
    for key, value in qos.items():
        for curve, attrs in value.items():
            if attrs.get('m1') == 0:
                del attrs['m1']
            if attrs.get('d') == 0:
                del attrs['d']
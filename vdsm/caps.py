#
# Copyright 2011-2016 Red Hat, Inc.
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

"""Collect host capabilities"""

import itertools
import os
import platform
import logging
import time
import linecache
import glob
import re
import xml.etree.ElementTree as ET
from distutils.version import LooseVersion

import libvirt

from vdsm.config import config
from vdsm import cpuarch
from vdsm import cpuinfo
from vdsm import dsaversion
from vdsm import hooks
from vdsm import libvirtconnection
from vdsm import netinfo
from vdsm import host
from vdsm import commands
from vdsm import utils
import storage.hba
from virt import vmdevices

# For debian systems we can use python-apt if available
try:
    import apt
    python_apt = True
except ImportError:
    python_apt = False

# For systems without rpm support
try:
    import rpm
except ImportError:
    pass

PAGE_SIZE_BYTES = os.sysconf('SC_PAGESIZE')
CPU_MAP_FILE = '/usr/share/libvirt/cpu_map.xml'

try:
    from gluster.api import GLUSTER_RPM_PACKAGES
    from gluster.api import GLUSTER_DEB_PACKAGES
    from gluster.api import glusterAdditionalFeatures
    _glusterEnabled = True
except ImportError:
    _glusterEnabled = False


class OSName:
    UNKNOWN = 'unknown'
    OVIRT = 'oVirt Node'
    RHEL = 'RHEL'
    FEDORA = 'Fedora'
    RHEVH = 'RHEV Hypervisor'
    DEBIAN = 'Debian'
    POWERKVM = 'PowerKVM'


class AutoNumaBalancingStatus:
    DISABLE = 0
    ENABLE = 1
    UNKNOWN = 2


class CpuTopology(object):
    def __init__(self, capabilities=None):
        self._topology = _getCpuTopology(capabilities)

    def threads(self):
        return self._topology['threads']

    def cores(self):
        return self._topology['cores']

    def sockets(self):
        return self._topology['sockets']

    def onlineCpus(self):
        return self._topology['onlineCpus']


class KdumpStatus(object):
    UNKNOWN = -1
    DISABLED = 0
    ENABLED = 1


def _getFreshCapsXMLStr():
    return libvirtconnection.get().getCapabilities()


@utils.memoized
def _getCapsXMLStr():
    return _getFreshCapsXMLStr()


def _getCpuTopology(capabilities):
    if capabilities is None:
        capabilities = _getFreshCapsXMLStr()

    caps = ET.fromstring(capabilities)
    host = caps.find('host')
    cells = host.find('.//cells')

    sockets = set()
    siblings = set()
    onlineCpus = []

    for cpu in cells.iter(tag='cpu'):
        if cpu.get('socket_id') is not None and \
           cpu.get('siblings') is not None:
            onlineCpus.append(cpu.get('id'))
            sockets.add(cpu.get('socket_id'))
            siblings.add(cpu.get('siblings'))

    topology = {'sockets': len(sockets),
                'cores': len(siblings),
                'threads': len(onlineCpus),
                'onlineCpus': onlineCpus}

    return topology


def _findLiveSnapshotSupport(guest):
    '''
    Returns the status of the live snapshot support
    on the hypervisor (QEMU).

    param guest:
    the `guest' XML element of the libvirt capabilities XML

    Return type: None or boolean.
    None if libvirt does not report the live
    snapshot support (as in version <= 1.2.2),
    '''
    features = guest.find('features')
    if not features:
        return None

    for feature in features.iter(tag='disksnapshot'):
        value = feature.get('default')
        if value.lower() == 'on':
            return True
        else:
            return False
    # libvirt < 1.2.2 does not export this information.
    return None


@utils.memoized
def _getLiveSnapshotSupport(arch, capabilities=None):
    if capabilities is None:
        capabilities = _getCapsXMLStr()
    caps = ET.fromstring(capabilities)

    for guestTag in caps.iter(tag='guest'):
        archTag = guestTag.find('arch')
        if archTag.get('name') == arch:
            return _findLiveSnapshotSupport(guestTag)

    return None


@utils.memoized
def getLiveMergeSupport():
    """
    Determine if libvirt provides the necessary features to enable live merge.
    We check for the existence of several libvirt flags to serve as indicators:

    VIR_DOMAIN_BLOCK_COMMIT_RELATIVE indicates that libvirt can maintain
    relative backing file path names when rewriting a backing chain.

    VIR_DOMAIN_EVENT_ID_BLOCK_JOB_2 indicates that libvirt can pass a drive
    name (ie. vda) rather than a path to the block job event callback.

    VIR_DOMAIN_BLOCK_COMMIT_ACTIVE indicates that libvirt supports merging the
    active layer using the virDomainBlockCommit API.
    """
    for flag in ('VIR_DOMAIN_BLOCK_COMMIT_RELATIVE',
                 'VIR_DOMAIN_EVENT_ID_BLOCK_JOB_2',
                 'VIR_DOMAIN_BLOCK_COMMIT_ACTIVE'):
        if not hasattr(libvirt, flag):
            logging.debug("libvirt is missing '%s': live merge disabled", flag)
            return False
    return True


@utils.memoized
def getNumaTopology(capabilities=None):
    if capabilities is None:
        capabilities = _getCapsXMLStr()
    caps = ET.fromstring(capabilities)
    host = caps.find('host')
    cells = host.find('.//cells')
    cellsInfo = {}
    cellSets = cells.findall('cell')
    for cell in cellSets:
        cellInfo = {}
        cpus = []
        for cpu in cell.iter(tag='cpu'):
            cpus.append(int(cpu.get('id')))
        cellInfo['cpus'] = cpus
        cellIndex = cell.get('id')
        memInfo = getMemoryStatsByNumaCell(int(cellIndex))
        cellInfo['totalMemory'] = memInfo['total']
        cellsInfo[cellIndex] = cellInfo
    return cellsInfo


def getMemoryStatsByNumaCell(cell):
    """
    Get the memory stats of a specified numa node, the unit is MiB.

    :param cell: the index of numa node
    :type cell: int
    :return: dict like {'total': '49141', 'free': '46783'}
    """
    cellMemInfo = libvirtconnection.get().getMemoryStats(cell, 0)
    cellMemInfo['total'] = str(cellMemInfo['total'] / 1024)
    cellMemInfo['free'] = str(cellMemInfo['free'] / 1024)
    return cellMemInfo


@utils.memoized
def getNumaNodeDistance():
    nodeDistance = {}
    retcode, out, err = commands.execCmd(['numactl', '--hardware'])
    if retcode != 0:
        logging.error("Get error when execute numactl", exc_info=True)
        return nodeDistance
    pattern = re.compile(r'\s+(\d+):(.*)')
    for item in out:
        match = pattern.match(item)
        if match:
            nodeDistance[match.group(1)] = map(int,
                                               match.group(2).strip().split())
    return nodeDistance


@utils.memoized
def getAutoNumaBalancingInfo():
    retcode, out, err = commands.execCmd(['sysctl', '-n', '-e',
                                          'kernel.numa_balancing'])
    if not out:
        return AutoNumaBalancingStatus.UNKNOWN
    elif out[0] == '0':
        return AutoNumaBalancingStatus.DISABLE
    elif out[0] == '1':
        return AutoNumaBalancingStatus.ENABLE
    else:
        return AutoNumaBalancingStatus.UNKNOWN


def _get_emulated_machines_from_node(node):
    # We have to make sure to inspect 'canonical' attribute where
    # libvirt puts the real machine name. Relevant bug:
    # https://bugzilla.redhat.com/show_bug.cgi?id=1229666
    return list(set((itertools.chain.from_iterable(
        (
            (m.text, m.get('canonical'))
            if m.get('canonical') else
            (m.text,)
        )
        for m in node.iterfind('machine')))))


def _get_emulated_machines_from_arch(arch, caps):
    arch_tag = caps.find('.//guest/arch[@name="%s"]' % arch)
    if not arch_tag:
        logging.error('Error while looking for architecture '
                      '"%s" in libvirt capabilities', arch)
        return []

    return _get_emulated_machines_from_node(arch_tag)


def _get_emulated_machines_from_domain(arch, caps):
    domain_tag = caps.find(
        './/guest/arch[@name="%s"]/domain[@type="kvm"]' % arch)
    if not domain_tag:
        logging.error('Error while looking for kvm domain (%s) '
                      'libvirt capabilities', arch)
        return []

    return _get_emulated_machines_from_node(domain_tag)


@utils.memoized
def _getEmulatedMachines(arch, capabilities=None):
    if capabilities is None:
        capabilities = _getCapsXMLStr()
    caps = ET.fromstring(capabilities)

    # machine list from domain can legally be empty
    # (e.g. only qemu-kvm installed)
    # in that case it is fine to use machines list from arch
    return (_get_emulated_machines_from_domain(arch, caps) or
            _get_emulated_machines_from_arch(arch, caps))


def _getAllCpuModels(capfile=CPU_MAP_FILE, arch=None):

    with open(capfile) as xml:
        cpu_map = ET.fromstring(xml.read())

    if arch is None:
        arch = platform.machine()

    # In libvirt CPU map XML, both x86_64 and x86 are
    # the same architecture, so in order to find all
    # the CPU models for this architecture, 'x86'
    # must be used
    if cpuarch.is_x86(arch):
        arch = 'x86'

    if cpuarch.is_ppc(arch):
        arch = 'ppc64'

    architectureElement = None

    architectureElements = cpu_map.findall('arch')

    if architectureElements:
        for a in architectureElements:
            if a.get('name') == arch:
                architectureElement = a

    if architectureElement is None:
        logging.error('Error while getting all CPU models: the host '
                      'architecture is not supported', exc_info=True)
        return {}

    allModels = dict()

    for m in architectureElement.findall('model'):
        element = m.find('vendor')
        if element is not None:
            vendor = element.get('name')
        else:
            element = m.find('model')
            if element is None:
                vendor = None
            else:
                elementName = element.get('name')
                vendor = allModels.get(elementName, None)
        allModels[m.get('name')] = vendor
    return allModels


@utils.memoized
def _getCompatibleCpuModels():
    c = libvirtconnection.get()
    allModels = _getAllCpuModels()

    def compatible(model, vendor):
        if not vendor:
            return False
        xml = '<cpu match="minimum"><model>%s</model>' \
              '<vendor>%s</vendor></cpu>' % (model, vendor)
        try:
            return c.compareCPU(xml, 0) in (libvirt.VIR_CPU_COMPARE_SUPERSET,
                                            libvirt.VIR_CPU_COMPARE_IDENTICAL)
        except libvirt.libvirtError as e:
            # hack around libvirt BZ#795836
            if e.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                return False
            raise

    return ['model_' + model for (model, vendor)
            in allModels.iteritems() if compatible(model, vendor)]


def _parseKeyVal(lines, delim='='):
    d = {}
    for line in lines:
        kv = line.split(delim, 1)
        if len(kv) != 2:
            continue
        k, v = map(str.strip, kv)
        d[k] = v
    return d


def _getIscsiIniName():
    try:
        with open('/etc/iscsi/initiatorname.iscsi') as f:
            return _parseKeyVal(f)['InitiatorName']
    except:
        logging.error('reporting empty InitiatorName', exc_info=True)
    return ''


def _getKdumpStatus():
    try:
        # check if kdump service is running
        with open('/sys/kernel/kexec_crash_loaded', 'r') as f:
            kdumpStatus = int(f.read().strip('\n'))

        if kdumpStatus == KdumpStatus.ENABLED:
            # check if fence_kdump is configured
            kdumpStatus = KdumpStatus.DISABLED
            with open('/etc/kdump.conf', 'r') as f:
                for line in f:
                    if line.startswith('fence_kdump_nodes'):
                        kdumpStatus = KdumpStatus.ENABLED
                        break
    except (IOError, OSError, ValueError):
        kdumpStatus = KdumpStatus.UNKNOWN
        logging.debug(
            'Error detecting fence_kdump configuration status',
            exc_info=True,
        )
    return kdumpStatus


@utils.memoized
def getos():
    if os.path.exists('/etc/rhev-hypervisor-release'):
        return OSName.RHEVH
    elif glob.glob('/etc/ovirt-node-*-release'):
        return OSName.OVIRT
    elif os.path.exists('/etc/fedora-release'):
        return OSName.FEDORA
    elif os.path.exists('/etc/redhat-release'):
        return OSName.RHEL
    elif os.path.exists('/etc/debian_version'):
        return OSName.DEBIAN
    elif os.path.exists('/etc/ibm_powerkvm-release'):
        return OSName.POWERKVM
    else:
        return OSName.UNKNOWN


@utils.memoized
def osversion():
    version = release = ''

    osname = getos()
    try:
        if osname == OSName.RHEVH or osname == OSName.OVIRT:
            d = _parseKeyVal(file('/etc/default/version'))
            version = d.get('VERSION', '')
            release = d.get('RELEASE', '')
        elif osname == OSName.DEBIAN:
            version = linecache.getline('/etc/debian_version', 1).strip("\n")
            release = ""  # Debian just has a version entry
        else:
            if osname == OSName.POWERKVM:
                release_path = '/etc/ibm_powerkvm-release'
            else:
                release_path = '/etc/redhat-release'

            ts = rpm.TransactionSet()
            for er in ts.dbMatch('basenames', release_path):
                version = er['version']
                release = er['release']
    except:
        logging.error('failed to find version/release', exc_info=True)

    return dict(release=release, version=version, name=osname)


def _getSELinux():
    selinux = dict()
    selinux['mode'] = str(utils.get_selinux_enforce_mode())

    return selinux


def _getHostdevPassthorughSupport():
    try:
        iommu_groups_exist = bool(len(os.listdir('/sys/kernel/iommu_groups')))
        if cpuarch.is_ppc(cpuarch.real()):
            return iommu_groups_exist

        dmar_exists = bool(len(os.listdir('/sys/class/iommu')))
        return iommu_groups_exist and dmar_exists
    except OSError:
        return False


def get():
    caps = {}

    caps['kvmEnabled'] = str(os.path.exists('/dev/kvm')).lower()

    cpuTopology = CpuTopology()
    if config.getboolean('vars', 'report_host_threads_as_cores'):
        caps['cpuCores'] = str(cpuTopology.threads())
    else:
        caps['cpuCores'] = str(cpuTopology.cores())

    caps['cpuThreads'] = str(cpuTopology.threads())
    caps['cpuSockets'] = str(cpuTopology.sockets())
    caps['onlineCpus'] = ','.join(cpuTopology.onlineCpus())
    caps['cpuSpeed'] = cpuinfo.frequency()
    caps['cpuModel'] = cpuinfo.model()
    caps['cpuFlags'] = ','.join(cpuinfo.flags() + _getCompatibleCpuModels())

    caps.update(_getVersionInfo())

    # TODO: Version requests by engine to ease handling of compatibility.
    netinfo_data = netinfo.cache.get(compatibility=30600)
    caps.update(netinfo_data)

    try:
        caps['hooks'] = hooks.installed()
    except:
        logging.debug('not reporting hooks', exc_info=True)

    caps['operatingSystem'] = osversion()
    caps['uuid'] = host.uuid()
    caps['packages2'] = _getKeyPackages()
    caps['emulatedMachines'] = _getEmulatedMachines(cpuarch.effective())
    caps['ISCSIInitiatorName'] = _getIscsiIniName()
    caps['HBAInventory'] = storage.hba.HBAInventory()
    caps['vmTypes'] = ['kvm']

    caps['memSize'] = str(utils.readMemInfo()['MemTotal'] / 1024)
    caps['reservedMem'] = str(config.getint('vars', 'host_mem_reserve') +
                              config.getint('vars', 'extra_mem_reserve'))
    caps['guestOverhead'] = config.get('vars', 'guest_ram_overhead')

    # Verify that our libvirt supports virtio RNG (since 10.0.2-31)
    requiredVer = LooseVersion('0.10.2-31')
    if 'libvirt' not in caps['packages2']:
        libvirtVer = None
    else:
        libvirtVer = LooseVersion(
            '-'.join((caps['packages2']['libvirt']['version'],
                      caps['packages2']['libvirt']['release'])))

    if libvirtVer is None:
        logging.debug('VirtioRNG DISABLED: unknown libvirt version')
    elif libvirtVer < requiredVer:
        logging.debug('VirtioRNG DISABLED: libvirt version %s required >= %s',
                      libvirtVer, requiredVer)
    else:
        caps['rngSources'] = vmdevices.core.Rng.available_sources()

    caps['numaNodes'] = getNumaTopology()
    caps['numaNodeDistance'] = getNumaNodeDistance()
    caps['autoNumaBalancing'] = getAutoNumaBalancingInfo()

    caps['selinux'] = _getSELinux()

    liveSnapSupported = _getLiveSnapshotSupport(cpuarch.effective())
    if liveSnapSupported is not None:
        caps['liveSnapshot'] = str(liveSnapSupported).lower()
    caps['liveMerge'] = str(getLiveMergeSupport()).lower()
    caps['kdumpStatus'] = _getKdumpStatus()

    caps['hostdevPassthrough'] = str(_getHostdevPassthorughSupport()).lower()
    caps['additionalFeatures'] = []
    if _glusterEnabled:
        caps['additionalFeatures'].extend(glusterAdditionalFeatures())
    return caps


def _dropVersion(vstring, logMessage):
    logging.error(logMessage)

    from distutils.version import StrictVersion
    # Drop cluster supported version to be strictly less than given vstring.
    info = dsaversion.version_info.copy()
    maxVer = StrictVersion(vstring)
    info['clusterLevels'] = [ver for ver in info['clusterLevels']
                             if StrictVersion(ver) < maxVer]
    return info


@utils.memoized
def _getVersionInfo():
    if not hasattr(libvirt, 'VIR_MIGRATE_ABORT_ON_ERROR'):
        return _dropVersion('3.4',
                            'VIR_MIGRATE_ABORT_ON_ERROR not found in libvirt,'
                            ' support for clusterLevel >= 3.4 is disabled.'
                            ' For Fedora 19 users, please consider upgrading'
                            ' libvirt from the virt-preview repository')

    if not hasattr(libvirt, 'VIR_MIGRATE_AUTO_CONVERGE'):
        return _dropVersion('3.6',
                            'VIR_MIGRATE_AUTO_CONVERGE not found in libvirt,'
                            ' support for clusterLevel >= 3.6 is disabled.'
                            ' For Fedora 20 users, please consider upgrading'
                            ' libvirt from the virt-preview repository')

    if not hasattr(libvirt, 'VIR_MIGRATE_COMPRESSED'):
        return _dropVersion('3.6',
                            'VIR_MIGRATE_COMPRESSED not found in libvirt,'
                            ' support for clusterLevel >= 3.6 is disabled.'
                            ' For Fedora 20 users, please consider upgrading'
                            ' libvirt from the virt-preview repository')

    return dsaversion.version_info


def _getKeyPackages():
    def kernelDict():
        try:
            ret = os.uname()
            ver, rel = ret[2].split('-', 1)
        except:
            logging.error('kernel release not found', exc_info=True)
            ver, rel = '0', '0'
        try:
            t = ret[3].split()[2:]
            del t[4]  # Delete timezone
            t = time.mktime(time.strptime(' '.join(t)))
        except:
            logging.error('kernel build time not found', exc_info=True)
            t = '0'
        return dict(version=ver, release=rel, buildtime=t)

    pkgs = {'kernel': kernelDict()}

    if getos() in (OSName.RHEVH, OSName.OVIRT, OSName.FEDORA, OSName.RHEL,
                   OSName.POWERKVM):
        KEY_PACKAGES = {
            'glusterfs-cli': ('glusterfs-cli',),
            'librbd1': ('librbd1',),
            'libvirt': ('libvirt', 'libvirt-daemon-kvm'),
            'mom': ('mom',),
            'qemu-img': ('qemu-img', 'qemu-img-rhev', 'qemu-img-ev'),
            'qemu-kvm': ('qemu-kvm', 'qemu-kvm-rhev', 'qemu-kvm-ev'),
            'spice-server': ('spice-server',),
            'vdsm': ('vdsm',),
        }

        if _glusterEnabled:
            KEY_PACKAGES.update(GLUSTER_RPM_PACKAGES)

        try:
            ts = rpm.TransactionSet()

            for pkg, names in KEY_PACKAGES.iteritems():
                try:
                    mi = itertools.chain(*[ts.dbMatch('name', name)
                                           for name in names]).next()
                except StopIteration:
                    logging.debug("rpm package %s not found",
                                  KEY_PACKAGES[pkg])
                else:
                    pkgs[pkg] = {
                        'version': mi['version'],
                        'release': mi['release'],
                        'buildtime': mi['buildtime'],
                    }
        except:
            logging.error('', exc_info=True)

    elif getos() == OSName.DEBIAN and python_apt:
        KEY_PACKAGES = {
            'glusterfs-cli': 'glusterfs-cli',
            'librbd1': 'librbd1',
            'libvirt': 'libvirt0',
            'mom': 'mom',
            'qemu-img': 'qemu-utils',
            'qemu-kvm': 'qemu-kvm',
            'spice-server': 'libspice-server1',
            'vdsm': 'vdsmd',
        }

        if _glusterEnabled:
            KEY_PACKAGES.update(GLUSTER_DEB_PACKAGES)

        cache = apt.Cache()

        for pkg in KEY_PACKAGES:
            try:
                deb_pkg = KEY_PACKAGES[pkg]
                ver = cache[deb_pkg].installed.version
                # Debian just offers a version
                pkgs[pkg] = dict(version=ver, release="", buildtime="")
            except:
                logging.error('', exc_info=True)

    return pkgs
#
# Copyright 2014-2017 Red Hat, Inc.
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

from contextlib import contextmanager
from itertools import tee, product
import logging
import socket
import threading
import uuid

import libvirt

from six.moves import range
from six.moves import zip

from vdsm.common import exception
from vdsm.common import response
from vdsm.config import config
from vdsm.virt import migration
from vdsm.virt import vmstatus

from monkeypatch import MonkeyPatchScope
from testlib import VdsmTestCase as TestCaseBase
from testlib import permutations, expandPermutations
from testlib import make_config
import vmfakelib as fake


# defaults
_DOWNTIME = config.getint('vars', 'migration_downtime')

_STEPS = config.getint('vars', 'migration_downtime_steps')

_STEPS_MIN = 2
_STEPS_HUGE = 1000

_DOWNTIME_MIN = 100
_DOWNTIME_HUGE = 10000

_PARAMS = tuple(product((_DOWNTIME_MIN, _DOWNTIME, _DOWNTIME_HUGE),
                        (_STEPS_MIN, _STEPS, _STEPS_HUGE)))


@expandPermutations
class DowntimeThreadTests(TestCaseBase):

    # No special meaning, But steps just need to be >= 2
    DOWNTIME = 1000

    @permutations([[1], [2], [10]])
    def test_update_downtime_using_n_steps(self, steps):
        downtimes = _update_downtime_repeatedly(self.DOWNTIME, steps)
        self.assertEqual(len(downtimes), steps)

    @permutations([[1], [2], [10]])
    def test_update_downtime_monotonic_increasing(self, steps):
        downtimes = _update_downtime_repeatedly(self.DOWNTIME, steps)
        self.assertTrue(sorted(downtimes), downtimes)

    @permutations([[1], [2], [10]])
    def test_update_downtime_converges(self, steps):
        downtimes = _update_downtime_repeatedly(self.DOWNTIME, steps)
        self.assertEqual(downtimes[-1], self.DOWNTIME)


@expandPermutations
class TestVmMigrationDowntimeSequence(TestCaseBase):

    @permutations(_PARAMS)
    def test_downtime_is_sequence(self, dtime, steps):
        self.assertTrue(len(self._default(dtime, steps)) >= 2)

    @permutations(_PARAMS)
    def test_downtime_increasing(self, dtime, steps):
        for a, b in pairwise(self._default(dtime, steps)):
            self.assertTrue(a <= b)

    @permutations(_PARAMS)
    def test_exponential_dowtime_never_zero(self, dtime, steps):
        for dt in self._default(dtime, steps):
            self.assertTrue(dt > 0)

    @permutations(_PARAMS)
    def test_exponential_downtime_is_lower(self, dtime, steps):
        # it's OK if exponential starts a little higher than linear...
        exp = self._default(dtime, steps)
        lin = self._linear(dtime, steps)
        self.assertAlmostEqual(exp[0], lin[0],
                               delta=self._delta(dtime, steps))

        # ...but what matters is that after that, it stays lower.
        for i, (a, b) in enumerate(zip(exp[1:], lin[1:])):
            msg = 'step=%i/%i exp=%f lin=%f' % (i + 1, steps, a, b)
            self.assertTrue(a <= b, msg)

    @permutations(_PARAMS)
    def test_exponential_same_end_value(self, dtime, steps):
        exp = self._default(dtime, steps)
        lin = self._linear(dtime, steps)
        self.assertAlmostEqual(exp[-1], lin[-1],
                               delta=self._delta(dtime, steps))

    @permutations(_PARAMS)
    def test_end_value_is_maximum(self, dtime, steps):
        exp = self._default(dtime, steps)
        self.assertAlmostEqual(exp[-1], dtime,
                               delta=self._delta(dtime, steps))

    # helpers

    def _delta(self, downtime, steps):
        """
        for near-equality checks. One tenth of one step to be sure.
        However, downtime is in milliseconds, so it is fair to
        have a lower bound here.
        """
        return max(1, (downtime / steps) / 10.)

    def _default(self, downtime, steps):
        """provides the default downtime sequence"""
        return list(migration.exponential_downtime(downtime, steps))

    def _linear(self, downtime, steps):
        return list(_linear_downtime(downtime, steps))


class MigrationParamsTests(TestCaseBase):

    def setUp(self):
        # random values, no real meaning
        self.params = {
            'foo': 'bar',
            'answer': 42,
            'hyperv': ['qemu', 'kvm'],
        }

    def test_params_stored(self):
        with fake.VM() as testvm:
            with testvm.migration_parameters(self.params):
                self.assertEqual(testvm.conf['_migrationParams'],
                                 self.params)

    def test_params_removed(self):
        with fake.VM() as testvm:
            with testvm.migration_parameters(self.params):
                pass

            self.assertNotIn('_migrationParams', testvm.conf)


@expandPermutations
class TestProgress(TestCaseBase):

    def setUp(self):
        self.job_stats = {
            'type': libvirt.VIR_DOMAIN_JOB_UNBOUNDED,
            libvirt.VIR_DOMAIN_JOB_TIME_ELAPSED: 42,
            libvirt.VIR_DOMAIN_JOB_DATA_TOTAL: 8192,
            libvirt.VIR_DOMAIN_JOB_DATA_PROCESSED: 0,
            libvirt.VIR_DOMAIN_JOB_DATA_REMAINING: 8192,
            libvirt.VIR_DOMAIN_JOB_MEMORY_TOTAL: 1024,
            libvirt.VIR_DOMAIN_JOB_MEMORY_PROCESSED: 512,
            libvirt.VIR_DOMAIN_JOB_MEMORY_REMAINING: 512,
            libvirt.VIR_DOMAIN_JOB_MEMORY_BPS: 128,
            libvirt.VIR_DOMAIN_JOB_MEMORY_CONSTANT: 0,
            libvirt.VIR_DOMAIN_JOB_COMPRESSION_BYTES: 0,
            # available since libvirt 1.3
            'memory_dirty_rate': 2,
            # available since libvirt 1.3
            'memory_iteration': 0,
        }
        # available since libvirt 3.2
        if getattr(libvirt, 'VIR_DOMAIN_JOB_OPERATION_MIGRATION_OUT', None):
            self.job_stats['operation'] = \
                libvirt.VIR_DOMAIN_JOB_OPERATION_MIGRATION_OUT

    def test___str__(self):
        prog = migration.Progress.from_job_stats(self.job_stats)
        self.assertNotRaises(str, prog)

    @permutations([
        # fields
        [(libvirt.VIR_DOMAIN_JOB_DATA_TOTAL,)],
        [(libvirt.VIR_DOMAIN_JOB_DATA_PROCESSED,)],
        [(libvirt.VIR_DOMAIN_JOB_DATA_REMAINING,)],
        [(libvirt.VIR_DOMAIN_JOB_MEMORY_TOTAL,)],
        [(libvirt.VIR_DOMAIN_JOB_MEMORY_PROCESSED,)],
        [(libvirt.VIR_DOMAIN_JOB_MEMORY_REMAINING,)],
    ])
    def test_job_stats_required_fields(self, fields):
        for field in fields:
            del self.job_stats[field]
        self.assertRaises(KeyError,
                          migration.Progress.from_job_stats,
                          self.job_stats)

    @permutations([
        # fields
        [(libvirt.VIR_DOMAIN_JOB_MEMORY_BPS,)],
        [(libvirt.VIR_DOMAIN_JOB_MEMORY_CONSTANT,)],
        [(libvirt.VIR_DOMAIN_JOB_COMPRESSION_BYTES,)],
        [('memory_dirty_rate',)],
        [('memory_iteration',)],
    ])
    def test___str___without_optional_fields(self, fields):
        for field in fields:
            del self.job_stats[field]
        prog = migration.Progress.from_job_stats(self.job_stats)
        self.assertNotRaises(str, prog)

    @permutations([
        # data_remaining, data_total, progress
        [0, 0, 0],
        [0, 100, 100],
        [100, 100, 0],
        [50, 100, 50],
        [33, 100, 67],
        [1, 100, 99],
        [99, 100, 1],
    ])
    def test_percentage(self, data_remaining, data_total, progress):
        self.job_stats[libvirt.VIR_DOMAIN_JOB_DATA_REMAINING] = data_remaining
        self.job_stats[libvirt.VIR_DOMAIN_JOB_DATA_TOTAL] = data_total
        prog = migration.Progress.from_job_stats(self.job_stats)
        self.assertEqual(prog.percentage, progress)

    @permutations([
        # job_type, ongoing
        # not sure could actually happen
        [libvirt.VIR_DOMAIN_JOB_BOUNDED, True],
        [libvirt.VIR_DOMAIN_JOB_UNBOUNDED, True],
        [libvirt.VIR_DOMAIN_JOB_NONE, False],
    ])
    def test_ongoing(self, job_type, ongoing):
        self.job_stats['type'] = job_type
        self.assertEqual(migration.ongoing(self.job_stats), ongoing)


@expandPermutations
class TestVmMigrate(TestCaseBase):

    def setUp(self):
        self.cif = fake.ClientIF()
        self.serv = fake.JsonRpcServer()
        self.cif.bindings["jsonrpc"] = self.serv

    @permutations([[vmstatus.UP]])
    def test_migrate_from_status(self, vm_status):
            with MonkeyPatchScope([
                (migration, 'SourceThread', fake.MigrationSourceThread)
            ]):
                with fake.VM(status=vm_status, cif=self.cif) as testvm:
                    res = testvm.migrate({})  # no params needed
                    self.assertFalse(response.is_error(res))

    @permutations([
        # vm_status, exception
        [vmstatus.WAIT_FOR_LAUNCH, exception.NoSuchVM],
        [vmstatus.DOWN, exception.NoSuchVM],
    ])
    def test_migrate_from_status_error(self, vm_status, exc):
            with MonkeyPatchScope([
                (migration, 'SourceThread', fake.MigrationSourceThread)
            ]):
                with fake.VM(status=vm_status, cif=self.cif) as testvm:
                    with self.assertRaises(exc):
                        testvm.migrate({})  # no params needed


class TestPostCopy(TestCaseBase):

    def test_post_copy_status(self):
        with fake.VM(status=vmstatus.MIGRATION_SOURCE,
                     post_copy=migration.PostCopyPhase.RUNNING,
                     params={'vmType': 'kvm'}) as testvm:
            stats = testvm.getStats()
        self.assertEqual(stats['status'], vmstatus.PAUSED)


class FakeServer(object):

    def __init__(self, initial_failures=0, exc=None):
        self._initial_failures = initial_failures
        self._exc = exc
        self.attempts = 0

    def migrationCreate(self, params, limit):
        self.attempts += 1
        if self.attempts > self._initial_failures:
            return response.success()
        return self._exc.response()


class FakeMigratingDomain(object):

    def __init__(self):
        self.migrations = 0

    def XMLDesc(self, flags):
        return ''

    def migrateSetMaxDowntime(self, value, flags):
        pass

    def migrateToURI3(self, duri, params, flags):
        self.migrations += 1

    def isPersistent(self):
        return True


class FakeVM(object):

    def __init__(self, dom=None):
        self._dom = dom
        self.id = str(uuid.uuid4())
        self.log = logging.getLogger('test.migration.FakeVM')
        self.conf = {}
        self._mem_size_mb = 128
        self.hasSpice = True
        self.post_copy = migration.PostCopyPhase.NONE
        self.stopped_migrated_event_processed = threading.Event()
        self.stopped_migrated_event_processed.set()

    @contextmanager
    def migration_parameters(self, params):
        self.conf['_migrationParams'] = params
        try:
            yield
        finally:
            del self.conf['_migrationParams']

    def min_cluster_version(self, major, minor):
        return False

    def status(self):
        return self.conf

    def getStats(self):
        return {}

    def saveState(self):
        pass

    def setDownStatus(self, status, reason):
        pass

    def destroy(self):
        pass

    def mem_size_mb(self):
        return self._mem_size_mb

    def prepare_migration(self):
        pass

    def isPersistent(self):
        return True


class FakeProgress(object):

    def __init__(self):
        self.percentage = 0


class FakeMonitorThread(object):

    def __init__(self, prog):
        self.progress = prog


def make_env():
    dom = FakeMigratingDomain()
    src = migration.SourceThread(FakeVM(dom))
    src.remoteHost = '127.0.0.1'
    src._monitorThread = FakeMonitorThread(FakeProgress())
    src._setupVdsConnection = lambda: None
    src._setupRemoteMachineParams = lambda: None
    return dom, src


@expandPermutations
class SourceThreadTests(TestCaseBase):

    def test_progress_start(self):
        vm = FakeVM()
        src = migration.SourceThread(vm)
        self.assertEqual(src._progress, 0)

    # random increasing numbers, no special meaning
    @permutations([
        # steps
        [(42,)],
        [(12, 33)],
    ])
    def test_progress_update_on_get_stat(self, steps):
        vm = FakeVM()
        src = migration.SourceThread(vm)
        prog = FakeProgress()
        src._monitorThread = FakeMonitorThread(prog)

        for step in steps:
            prog.percentage = step
            self.assertEqual(src.getStat()['progress'], prog.percentage)

        self.assertEqual(src.getStat()['progress'], steps[-1])

    def test_progress_not_backwards(self):
        steps = [8, 15, 23, 85, 81]

        vm = FakeVM()
        src = migration.SourceThread(vm)
        prog = FakeProgress()
        src._monitorThread = FakeMonitorThread(prog)

        for step in steps:
            prog.percentage = step
            old_progress = src._progress
            self.assertGreaterEqual(src.getStat()['progress'], old_progress)

        self.assertEqual(src._progress, max(steps))

    @permutations([
        # failures
        [0],
        [1],
        [2],
        [10],
    ])
    def test_retry_on_limit_exceeded(self, failures):
        serv = FakeServer(initial_failures=failures,
                          exc=exception.MigrationLimitExceeded())
        dom, src = make_env()
        src._destServer = serv
        cfg = make_config([('vars', 'migration_retry_timeout', '0')])
        with MonkeyPatchScope([(migration, 'config', cfg)]):
            src.run()

        self.assertEqual(serv.attempts, failures + 1)  # +1 for success
        self.assertEqual(dom.migrations, 1)

    def test_do_not_retry_when_started(self):
        # we do not retry regardless of the last reported progress
        progress = 55
        serv = FakeServer()

        dom, src = make_env()
        src._destServer = serv
        src._finishSuccessfully = lambda: None
        src._progress = progress

        src.run()

        self.assertEqual(dom.migrations, 1)
        self.assertEqual(serv.attempts, 1)
        self.assertEqual(src.getStat()['progress'], progress)


# stolen^Wborrowed from itertools recipes
def pairwise(iterable):
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


def _linear_downtime(downtime, steps):
    "this is the old formula as reference"
    for i in range(steps):
        # however, it makes no sense to have less than 1 ms
        # we want to avoid anyway downtime = 0
        yield max(1, downtime * (i + 1) / steps)


def _update_downtime_repeatedly(downtime, steps):
        dom = fake.Domain()

        with fake.VM({'memSize': 1024}) as testvm:
            testvm._dom = dom

            cfg = make_config([('vars', 'migration_downtime_delay', '0')])
            with MonkeyPatchScope([(migration, 'config', cfg)]):
                dt = migration.DowntimeThread(testvm, downtime, steps)
                dt.set_initial_downtime()
                dt.start()
                dt.join()

                return dom.getDowntimes()


class CannonizeHostPortTest(TestCaseBase):

    def test_no_arguments(self):
        self._assert_is_ip_address_with_port(migration._cannonize_host_port())

    def test_none_argument(self):
        self._assert_is_ip_address_with_port(
            migration._cannonize_host_port(None))

    def test_none_argument_and_port(self):
        port = 65432
        res = migration._cannonize_host_port(None, port)
        self._assert_is_ip_address_with_port(res)
        # address must include the given port
        self.assertTrue(res.endswith(str(port)))

    def test_address_no_port(self):
        self._assert_is_ip_address_with_port(
            migration._cannonize_host_port('127.0.0.1'))

    def test_address_with_port(self):
        address = "127.0.0.1:65432"
        self.assertEqual(address, migration._cannonize_host_port(address))

    def test_address_with_port_parameter(self):
        addr = '127.0.0.1'
        port = 65432
        res = migration._cannonize_host_port(addr, port)
        self._assert_is_ip_address_with_port(res)
        # address must include the given port
        self.assertTrue(res.endswith(str(port)))

    def test_address_with_bad_port_parameter(self):
        addr = '127.0.0.1'
        port = '65432'
        self.assertRaises(TypeError,
                          migration._cannonize_host_port,
                          addr, port)

    def _assert_is_ip_address_with_port(self, addrWithPort):
        try:
            # to handle IPv6, we expect the \[ipv6\][:port] notation.
            # this split also gracefully handle ipv4:port notation.
            # details: http://tools.ietf.org/html/rfc5952#page-11
            # the following will handle all IP families:
            addr, port = addrWithPort.rsplit(':', 1)
        except ValueError:
            raise AssertionError('%s is not a valid IP address:' %
                                 addrWithPort)
        else:
            self._assert_valid_address(addr)
            self._assert_valid_port(port)

    def _assert_valid_address(self, addr):
        print(addr)
        if addr != 'localhost':
            if '.' in addr:
                if not _is_ipv4_address(addr):
                    raise AssertionError('invalid IPv4 address: %s',
                                         addr)
            elif ':' in addr:
                if not addr.startswith('[') or not addr.endswith(']'):
                    raise AssertionError('malformed IPv6 address: %s',
                                         addr)
                if not _is_ipv6_address(addr[1:-1]):
                    raise AssertionError('invalid IPv6 address: %s',
                                         addr)
            else:
                raise AssertionError('unrecognized IP address family: %s',
                                     addr)

    def _assert_valid_port(self, port_str):
        try:
            port = int(port_str)
        except ValueError:
            raise AssertionError('malformed port: %s' % port_str)
        if port <= 0 or port >= 2**16:
            raise AssertionError('malformed port: %s' % port_str)


def _is_ipv4_address(address):
    try:
        socket.inet_pton(socket.AF_INET, address)
    except socket.error:
        return False
    else:
        return True


def _is_ipv6_address(address):
    addr = address.split('/', 1)
    try:
        socket.inet_pton(socket.AF_INET6, addr[0])
    except socket.error:
        return False
    else:
        if len(addr) == 2:
            return _is_valid_prefix_len(addr[1])
        return True


def _is_valid_prefix_len(prefixlen):
    try:
        prefixlen = int(prefixlen)
        if prefixlen < 0 or prefixlen > 127:
            return False
    except ValueError:
        return False
    return True

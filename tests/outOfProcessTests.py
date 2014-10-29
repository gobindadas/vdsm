#
# Copyright 2012 Red Hat, Inc.
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

from testlib import VdsmTestCase as TestCaseBase
import storage.outOfProcess as oop
import os
import tempfile
import time
import re
from weakref import ref

from vdsm.config import config


class OopWrapperTests(TestCaseBase):
    IOPROC_IDLE_TIME = config.getint("irs", "max_ioprocess_idle_time")

    def setUp(self):
        oop.setDefaultImpl(oop.IOPROC)
        self.pool = oop.getGlobalProcPool()

    def testSamePoolName(self):
        poolA = "A"
        pids = []
        for pool in (poolA, poolA):
            proc = oop.getProcessPool(pool)._ioproc
            name = proc._commthread.getName()
            pids.append(int(re.search(r'\d+', name).group()))

        self.assertEquals(pids[0], pids[1])

    def testDifferentPoolName(self):
        poolA = "A"
        poolB = "B"
        pools = (poolA, poolB)
        pids = []
        for pool in pools:
            proc = oop.getProcessPool(pool)._ioproc
            name = proc._commthread.name
            pids.append(int(re.search(r'\d+', name).group()))

        self.assertNotEquals(pids[0], pids[1])

    def testAmountOfInstancesPerPoolName(self):
        poolA = "A"
        poolB = "B"
        wrapper = ref(oop.getProcessPool(poolA))
        ioproc = ref(oop.getProcessPool(poolA)._ioproc)
        oop.getProcessPool(poolA)
        time.sleep(self.IOPROC_IDLE_TIME + 1)
        oop.getProcessPool(poolB)
        self.assertEquals(wrapper(), None)
        import gc
        gc.collect()
        time.sleep(1)
        gc.collect()
        print "GARBAGE: ", gc.garbage
        refs = gc.get_referrers(ioproc())
        print refs
        print gc.get_referrers(*refs)
        self.assertEquals(ioproc(), None)

    def testEcho(self):
        data = """Censorship always defeats it own purpose, for it creates in
                  the end the kind of society that is incapable of exercising
                  real discretion."""
        # Henry Steele Commager

        self.assertEquals(self.pool._ioproc.echo(data), data)

    def testFileUtilsCall(self):
        """fileUtils is a custom module and calling it might break even though
        built in module calls arn't broken"""
        path = "/dev/null"
        self.assertEquals(self.pool.fileUtils.pathExists(path), True)

    def testSubModuleCall(self):
        path = "/dev/null"
        self.assertEquals(self.pool.os.path.exists(path), True)

    def testUtilsFuncs(self):
        tmpfd, tmpfile = tempfile.mkstemp()
        self.pool.utils.rmFile(tmpfile)
        os.close(tmpfd)
        return True

# Copyright (C) 2015-2022 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import
import unittest
import os
import sys
import time
import shutil

from toil.common import Toil
from toil.jobStores.abstractJobStore import (NoSuchJobStoreException,
                                             NoSuchFileException)
from toil.jobStores.utils import generate_locator
from toil import subprocess
from toil.test import ToilTest, needs_cwl, needs_aws_s3

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))  # noqa
sys.path.insert(0, pkg_root)  # noqa


class ToilKillTest(ToilTest):
    """A set of test cases for "toil kill"."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_store = os.path.join(os.getcwd(), 'testkill')

    def setUp(self):
        """Shared test variables."""
        self.cwl = os.path.abspath('src/toil/test/utils/ABCWorkflowDebug/sleep.cwl')
        self.yaml = os.path.abspath('src/toil/test/utils/ABCWorkflowDebug/sleep.yaml')

    def tearDown(self):
        """Default tearDown for unittest."""
        cmd = ['toil', 'clean', self.job_store]
        subprocess.check_call(cmd)

        if os.path.exists('tmp'):
            shutil.rmtree('tmp')
        unittest.TestCase.tearDown(self)

    @needs_cwl
    def test_cwl_toil_kill(self):
        """Test "toil kill" on a CWL workflow with a 100 second sleep."""

        run_cmd = ['toil-cwl-runner', '--jobStore', self.job_store, self.cwl, self.yaml]
        kill_cmd = ['toil', 'kill', self.job_store]

        # run the sleep workflow
        cwl_process = subprocess.Popen(run_cmd)

        # wait until workflow starts running
        while True:
            try:
                job_store = Toil.resumeJobStore(self.job_store)
                with job_store.read_shared_file_stream("pid.log") as _:
                    pass
                break
            except (NoSuchJobStoreException, NoSuchFileException):
                time.sleep(2)

        # run toil kill
        subprocess.check_call(kill_cmd)

        # after toil kill succeeds, the workflow should've exited
        assert cwl_process.poll() is None


@needs_aws_s3
class ToilKillTestWithAWSJobStore(ToilKillTest):
    """A set of test cases for "toil kill" using the AWS job store."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_store = generate_locator("aws", decoration="testkill")


if __name__ == "__main__":
    unittest.main()  # run all tests

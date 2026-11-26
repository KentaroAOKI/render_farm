import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.common import command_flags
from farm.process import ProcessGroup


@unittest.skipUnless(os.name == "nt", "Windows Job Object behavior")
class ProcessGroupTests(unittest.TestCase):
    def test_already_exited_child_needs_no_job_object(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"], **command_flags())
        self.assertEqual(process.wait(timeout=10), 0)
        group = ProcessGroup(process)
        self.assertIsNone(group.handle)
        group.stop()

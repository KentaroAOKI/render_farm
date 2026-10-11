from pathlib import Path
import sys
import unittest
import time
from unittest.mock import patch, Mock
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_core import CoreTests
from farm.worker import Supervisor
from farm.common import read_json, atomic_json
from farm.store import OwnershipLostError


class SupervisorTests(CoreTests):
    # Reuse fixture setup, not the core test cases.
    def run_fake(self, case, limit=1):
        jid = self.job(end=1, timeout_minutes=.02, restart_limit=limit)
        job = self.claim()
        supervisor = Supervisor(self.store.root, poll=.03,
            launch=lambda request: [sys.executable, "-B", str(Path(__file__).with_name("fake_maya.py")), str(Path(request["work"]) / "request.json"), case])
        supervisor.run_job(job, {"path": ".", "oiiotool": "unused"})
        return self.store.read()["jobs"][jid]

    def test_frozen_maya_restarts_and_publishes(self):
        job = self.run_fake("recover")
        self.assertEqual(job["state"], "complete", job.get("error"))
        self.assertEqual(list(job["restarts"].values()), [1])
        work = self.store.root / job["work"]
        self.assertEqual(read_json(work / "fake_starts.json")["count"], 2)
        self.assertTrue((self.store.root / job["publication"] / "manifest.json").exists())

    def test_restart_limit_retains_failed_work(self):
        job = self.run_fake("always_freeze", limit=1)
        self.assertEqual(job["state"], "failed")
        self.assertIn("再起動上限", job["error"])
        self.assertEqual(list(job["restarts"].values()), [1])
        self.assertTrue((self.store.root / job["work"] / "fake.log").exists())

    def test_render_error_is_not_retried_as_freeze(self):
        job = self.run_fake("error")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["restarts"], {})

    def test_direct_mode_uses_registered_project_without_materialization(self):
        with patch("farm.assets.materialize", side_effect=AssertionError("Project must not be copied")), \
             patch("farm.assets.create_snapshot", side_effect=AssertionError("Snapshot must not be created")):
            job = self.run_fake("recover")
        self.assertEqual("complete", job["state"], job.get("error"))
        work = self.store.root / job["work"]
        request = read_json(work / "request.json")
        self.assertEqual(str(self.project.resolve()), request["project"])
        self.assertEqual(str((self.project / "scenes/a.ma").resolve()), request["scene"])
        self.assertNotIn("sources", request)
        self.assertFalse((work / "sources").exists())
        self.assertFalse((self.store.root / "data/blobs").exists())

    def test_storage_recovery_keeps_same_claim(self):
        self.job()
        job = self.claim()
        supervisor = Supervisor(self.store.root)
        with patch.object(supervisor, "_run_job", side_effect=[OSError("disconnected"), "recovered"]) as attempt, patch("farm.worker.time.sleep"):
            self.assertEqual(supervisor.run_job(job, {}), "recovered")
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(attempt.call_args.args[0]["token"], job["token"])

    def test_idle_worker_reloads_new_postprocessing_configuration(self):
        jid = self.job(post={"video": True, "fastdvdnet": True})
        config = [{"enabled_versions": ["2027"], "ffmpeg": ""}]
        diagnostics = []
        def diagnose(root, current):
            diagnostics.append(dict(current))
            ready = bool(current.get("ffmpeg"))
            return {"2027": {"plugins": {"mtoa": "test"}, "gpu": True, "ffmpeg": ready, "fastdvdnet": ready}}, {}
        waits = []
        def idle_wait(seconds):
            waits.append(seconds)
            self.assertEqual(len(waits), 1, "New configuration did not allow the pending job to start")
            config[0] = {"enabled_versions": ["2027"], "ffmpeg": "bin/ffmpeg.exe", "fastdvdnet_python": "bin/python.exe"}
        with patch("farm.worker.worker_config", side_effect=lambda root: dict(config[0])), \
             patch("farm.worker.diagnose", side_effect=diagnose), patch("farm.worker.time", wraps=time) as clock:
            clock.sleep.side_effect = idle_wait
            supervisor = Supervisor(self.store.root)
            def complete(job, caps):
                self.assertTrue(caps["ffmpeg"] and caps["fastdvdnet"])
                self.store.finish(job["id"], job["token"], "complete")
                atomic_json(supervisor.stopfile, {"stop": True})
            with patch.object(supervisor, "run_job", side_effect=complete):
                supervisor.run()
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(self.store.read()["jobs"][jid]["state"], "complete")
        self.assertTrue(self.store.read()["workers"][supervisor.worker]["capabilities"]["2027"]["ffmpeg"])

    def test_invalid_token_is_not_retried_as_storage_failure(self):
        jid = self.job()
        job = self.claim()
        supervisor = Supervisor(self.store.root)
        with patch.object(supervisor, "_run_job", side_effect=OwnershipLostError("rejected token")), \
             patch("farm.worker.time.sleep", side_effect=AssertionError("Must not wait for storage")):
            self.assertEqual(supervisor.run_job(job, {}), "ownership_lost")
        self.assertEqual(read_json(supervisor.directory / "ownership_lost.json")["job"], jid)
        self.assertEqual(self.store.read()["jobs"][jid]["token"], job["token"])

    def test_losing_ownership_during_storage_wait_exits(self):
        jid = self.job()
        job = self.claim()
        supervisor = Supervisor(self.store.root)
        with patch.object(supervisor, "_run_job", side_effect=OSError("disconnected")), \
             patch.object(supervisor.store, "owned", side_effect=OwnershipLostError("another owner")), \
             patch("farm.worker.time.sleep") as sleep:
            self.assertEqual(supervisor.run_job(job, {}), "ownership_lost")
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(self.store.read()["jobs"][jid]["token"], job["token"])

    def test_queue_is_claimed_again_after_ownership_loss(self):
        jid = self.job()
        supervisor = Supervisor(self.store.root)
        tokens = []
        def attempt(job, caps):
            tokens.append(job['token'])
            if len(tokens) == 1:
                with self.store.transaction() as state:
                    state['jobs'][jid].update(state='pending', owner=None, token=None)
                raise OwnershipLostError('claim no longer current')
            self.store.finish(jid, job['token'], 'complete')
            atomic_json(supervisor.stopfile, {'stop': True})
        with patch('farm.worker.diagnose', return_value=({'2027': {'plugins': {'mtoa': 'test'}}}, {})), \
             patch.object(supervisor, '_run_job', side_effect=attempt):
            supervisor.run()
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertEqual(self.store.read()['jobs'][jid]['state'], 'complete')

    def test_monitor_does_not_swallow_ownership_rejection(self):
        self.job()
        job = self.claim()
        supervisor = Supervisor(self.store.root)
        with patch.object(supervisor.store, 'heartbeat', side_effect=OwnershipLostError('rejected')), \
             patch('farm.worker.time', wraps=time) as clock:
            clock.sleep.side_effect = AssertionError('Ownership errors must not enter the storage wait')
            with self.assertRaises(OwnershipLostError):
                supervisor.monitor(job, self.store.root / 'unused', Mock(pid=42), Mock(), 'attempt', {})


# Inherited cases already run in test_core.py; only run this class's new cases.
def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SupervisorTests(name) for name in SupervisorTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

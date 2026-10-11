from pathlib import Path
import unittest
import uuid

from farm.common import atomic_json, read_json
from farm.maya_entry import RenderSession
from farm.metrics import display_metrics, worker_unresponsive


class RenderMetricsTests(unittest.TestCase):
    def test_stale_worker_heartbeat_is_not_reported_as_live(self):
        now = 1000
        self.assertFalse(worker_unresponsive({'state': 'working', 'updated': now - 10}, now))
        self.assertTrue(worker_unresponsive({'state': 'working', 'updated': now - 90}, now))
        self.assertTrue(worker_unresponsive({'state': 'idle', 'updated': now - 90}, now))
        self.assertFalse(worker_unresponsive({'state': 'stopped', 'updated': now - 90}, now))
        self.assertTrue(worker_unresponsive(None, now))

    def test_older_supervisor_metrics_are_read_from_matching_maya_attempt(self):
        root = Path(__file__).parent / "runtime" / ("metrics_" + uuid.uuid4().hex)
        work = root / "workers/test/jobs/job"
        work.mkdir(parents=True)
        atomic_json(work / "maya_status.json", {"attempt": "current", "loading_seconds": 555.5,
                                               "max_frame_seconds": 741.4})
        job = {"work": "workers/test/jobs/job", "attempt": "current", "state": "rendering"}
        result = display_metrics(root, job)
        self.assertEqual(result["loading_seconds"], 555.5)
        self.assertEqual(result["max_frame_seconds"], 741.4)
        self.assertNotIn("loading_seconds", job)
        job["attempt"] = "different"
        self.assertNotIn("loading_seconds", display_metrics(root, job))

    def test_status_preserves_loading_and_longest_verified_frame_on_resume(self):
        work = Path(__file__).parent / "runtime" / ("metrics_" + uuid.uuid4().hex)
        work.mkdir(parents=True)
        atomic_json(work / "frames.json", {"a": {"seconds": 31.25}, "b": {"seconds": 82.5}})
        session = RenderSession({"work": str(work), "attempt": "test", "job": {"settings": {
            "layers": ["layer"], "cameras": ["camera"], "start": 1, "end": 3, "step": 1}}})
        session.loading_started = 100
        session.loading_seconds = 400.5
        session.status("rendering", unit="c")
        status = read_json(work / "maya_status.json")
        self.assertEqual(status["loading_seconds"], 400.5)
        self.assertEqual(status["max_frame_seconds"], 82.5)
        session.frames["c"] = {"seconds": 95.75}
        session.status("render_complete")
        self.assertEqual(read_json(work / "maya_status.json")["max_frame_seconds"], 95.75)

    def test_no_completed_frames_is_unmeasured(self):
        work = Path(__file__).parent / "runtime" / ("metrics_" + uuid.uuid4().hex)
        work.mkdir(parents=True)
        session = RenderSession({"work": str(work), "attempt": "test", "job": {"settings": {
            "layers": ["layer"], "cameras": ["camera"], "start": 1, "end": 1, "step": 1}}})
        session.status("opening")
        status = read_json(work / "maya_status.json")
        self.assertIsNone(status["loading_seconds"])
        self.assertIsNone(status["max_frame_seconds"])

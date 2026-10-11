from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from farm.common import atomic_json
from farm.store import Store
from farm.late_post import published_groups, run_late_postprocess


class LatePostTests(unittest.TestCase):
    def test_requires_published_verified_matching_job_and_png(self):
        base = Path(__file__).parent / "runtime"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            root = Path(folder)
            store = Store(root)
            with store.transaction() as state:
                state["jobs"]["job"] = {"id": "job", "state": "rendering"}
            with self.assertRaisesRegex(ValueError, "公開"):
                published_groups(store, "job")
            with store.transaction() as state:
                state["jobs"]["job"].update(publication="results/source", scene_id="scene")
            source = root / "results/source"
            source.mkdir(parents=True)
            manifest = {"verified": True, "job": {"id": "other", "settings": {"fps": "24"}},
                        "frames": {"one": {"frame": 1, "layer": "L", "camera": "C", "files": [{"pass": "beauty", "path": "raw/beauty.exr"}]}}}
            atomic_json(source / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "一致"):
                published_groups(store, "job")
            manifest["job"]["id"] = "job"
            atomic_json(source / "manifest.json", manifest)
            with patch("farm.late_post.check_dependencies") as diagnose:
                with self.assertRaisesRegex(ValueError, "PNG"):
                    run_late_postprocess(store, "job", {"video": True}, {})
                with self.assertRaisesRegex(ValueError, "対象"):
                    run_late_postprocess(store, "job", {"video": True}, {}, selection=[("wrong", "C")])
                with self.assertRaisesRegex(ValueError, "FPS"):
                    run_late_postprocess(store, "job", {"video": True}, {}, fps="0")
                diagnose.assert_not_called()
            self.assertNotIn("postprocess_runs", store.read()["jobs"]["job"])

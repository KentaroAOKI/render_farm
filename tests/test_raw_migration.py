from pathlib import Path
import tempfile
import unittest
from farm.common import atomic_json, read_json, sha256
from farm.images import migrate_raw_checkpoint, output_prefix, verify_record


class RawMigrationTests(unittest.TestCase):
    def test_migration_preserves_content_and_is_repeatable(self):
        base = Path(__file__).parent / "runtime"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            work = Path(folder)
            old = work / "raw/unit/beauty_1.png"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"original image contents")
            record = {"layer": "defaultRenderLayer", "camera": "|Cam|Shape", "frame": 1,
                      "seconds": 12.3, "signature": "preserved", "files": [{"path": "raw/unit/beauty_1.png", "pass": "beauty", "sha256": sha256(old)}]}
            atomic_json(work / "frames.json", {"unit": record})
            report = migrate_raw_checkpoint(work)
            self.assertEqual(report["moved"], 1)
            frames = read_json(work / "frames.json")
            verify_record(work, frames["unit"])
            self.assertEqual(frames["unit"]["seconds"], 12.3)
            self.assertEqual(frames["unit"]["signature"], "preserved")
            self.assertEqual(frames["unit"]["files"][0]["path"], "raw/" + output_prefix(record["layer"], record["camera"]) + "beauty.000001.png")
            self.assertFalse(old.exists())
            self.assertFalse(old.parent.exists())
            self.assertEqual(read_json(work / report["checkpoint_backup"]), {"unit": record})
            self.assertEqual(migrate_raw_checkpoint(work)["moved"], 0)

    def test_collision_keeps_original_and_checkpoint(self):
        base = Path(__file__).parent / "runtime"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            work = Path(folder)
            old = work / "raw/unit/beauty.png"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"original")
            record = {"layer": "L", "camera": "C", "frame": 10,
                      "files": [{"path": "raw/unit/beauty.png", "pass": "beauty", "sha256": sha256(old)}]}
            atomic_json(work / "frames.json", {"unit": record})
            (work / "raw" / (output_prefix("L", "C") + "beauty.000010.png")).write_bytes(b"different")
            with self.assertRaisesRegex(ValueError, "異なる画像"):
                migrate_raw_checkpoint(work)
            self.assertEqual(old.read_bytes(), b"original")
            self.assertEqual(read_json(work / "frames.json"), {"unit": record})

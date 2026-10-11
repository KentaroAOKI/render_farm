from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from farm.images import collect_outputs, flatten_outputs, verify_record
from farm.common import sha256


class ImageNameTests(unittest.TestCase):
    def test_flat_raw_names_separate_cameras_and_quarantine_unrecorded_output(self):
        work = Path(__file__).parent / "runtime" / ("flat_" + uuid.uuid4().hex)
        records = []
        for index, camera in enumerate(["|one|camera:Shape", "|two|camera_Shape", "|one|camera:Shape"]):
            directory = work / "rendering" / str(index)
            directory.mkdir(parents=True)
            source = directory / "beauty.000001.png"
            source.write_bytes(bytes([index]))
            files = [{"path": source.name, "pass": "beauty", "sha256": sha256(source)}]
            files = flatten_outputs(work, directory, files, "defaultRenderLayer", camera, str(index))
            verify_record(work, {"files": files})
            records.append(files[0]["path"])
        self.assertNotEqual(records[0], records[1])
        self.assertEqual(records[0], records[2])
        self.assertTrue(all(p.is_file() for p in (work / "raw").iterdir()))
        self.assertEqual(len(list((work / "raw").iterdir())), 2)
        self.assertEqual(len(list((work / "quarantine").rglob("*.png"))), 1)
        self.assertTrue(all(Path(p).parent.as_posix() == "raw" for p in records))

    def test_six_digit_frames_preserve_beauty_aovs_and_crypto_groups(self):
        base = Path(__file__).parent / "runtime" / ("names_" + uuid.uuid4().hex)
        settings = {"width": 1, "height": 1}
        info = {"width": 1, "height": 1, "channels": ["R", "G", "B"]}
        names = []
        for frame in [100, 2, 10]:
            folder = base / str(frame)
            folder.mkdir(parents=True)
            for name in ["RGBA.png", "N.exr", "crypto_object00.exr", "crypto_object01.exr"]:
                (folder / name).write_bytes(name.encode())
            with patch("farm.images.image_info", side_effect=lambda p, _: {**info, "sha256": sha256(p)}):
                records = collect_outputs(folder, settings, ["N", "crypto_object"], "fake", frame=frame)
            self.assertEqual({f"beauty.{frame:06d}.png", f"N.{frame:06d}.exr",
                              f"crypto_object00.{frame:06d}.exr", f"crypto_object01.{frame:06d}.exr"},
                             {r["path"] for r in records})
            for record in records:
                self.assertEqual(sha256(folder / record["path"]), record["sha256"])
            names.append(next(r["path"] for r in records if r["pass"] == "beauty"))
        self.assertEqual(sorted(names), ["beauty.000002.png", "beauty.000010.png", "beauty.000100.png"])

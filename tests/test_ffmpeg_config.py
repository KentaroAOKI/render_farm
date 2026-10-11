from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from farm.common import atomic_json
from farm.worker import worker_config


class FFmpegConfigTests(unittest.TestCase):
    def test_default_relative_and_explicit_paths(self):
        base = Path(__file__).parent / "runtime"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            root = Path(folder).resolve()
            worker = root / "workers/TestPC"
            worker.mkdir(parents=True)
            (root / "bin").mkdir()
            with patch("farm.worker.worker_dir", return_value=worker):
                self.assertFalse(worker_config(root).get("ffmpeg"))
                (root / "bin/ffmpeg.exe").write_bytes(b"fixture")
                self.assertEqual(worker_config(root)["ffmpeg"], str(root / "bin/ffmpeg.exe"))
                atomic_json(worker / "config.json", {"ffmpeg": "bin/custom.exe"})
                self.assertEqual(worker_config(root)["ffmpeg"], str(root / "bin/custom.exe"))
                external = str(root.parent / "explicit.exe")
                atomic_json(worker / "config.json", {"ffmpeg": external})
                self.assertEqual(worker_config(root)["ffmpeg"], external)

    def test_portable_fastdvdnet_defaults_and_relative_overrides(self):
        base = Path(__file__).parent / "runtime"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            root = Path(folder).resolve()
            worker = root / "workers/TestPC"
            worker.mkdir(parents=True)
            python = root / "bin/WinPython/WPy64-test/python/python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fixture")
            repo = root / "tools/fastdvdnet"
            repo.mkdir(parents=True)
            for name in ("models.py", "fastdvdnet.py", "model.pth"):
                (repo / name).write_bytes(b"fixture")
            with patch("farm.worker.worker_dir", return_value=worker):
                config = worker_config(root)
                self.assertEqual(config["fastdvdnet_python"], str(python))
                self.assertEqual(config["fastdvdnet_repo"], str(repo))
                atomic_json(worker / "config.json", {"fastdvdnet_python": "other/python.exe", "fastdvdnet_repo": "other/code", "fastdvdnet_model": "other/model.pth"})
                config = worker_config(root)
                self.assertEqual(config["fastdvdnet_python"], str(root / "other/python.exe"))
                self.assertEqual(config["fastdvdnet_repo"], str(root / "other/code"))
                self.assertEqual(config["fastdvdnet_model"], str(root / "other/model.pth"))

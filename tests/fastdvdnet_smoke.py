"""Verify real FastDVDnet inference and lossless video with the installed WinPython."""
from pathlib import Path
import json
import os
import subprocess
import sys
import uuid
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import atomic_json, read_json, runtime_env, sha256
from farm.worker import worker_config
from farm.post import check_dependencies

work = ROOT / "tests/runtime" / ("fastdvdnet_" + uuid.uuid4().hex[:8])
env = runtime_env(ROOT, work)
os.environ.update(env)
import numpy as np
import torch
from PIL import Image

config = worker_config(ROOT)
caps = check_dependencies(ROOT, config)
assert caps["ffmpeg"] and caps["fastdvdnet"], caps
assert torch.cuda.is_available(), "GPU not available"
source = work / "input"
source.mkdir(parents=True)
images = []
for i in range(3):
    rng = np.random.default_rng(i)
    pixels = rng.integers(0, 256, (65, 67, 3), dtype=np.uint8)
    path = source / ("beauty.%06d.png" % (i + 1))
    Image.fromarray(pixels).save(path)
    images.append(path)
hashes = [sha256(p) for p in images]
reports = {}
for device in ("gpu", "cpu"):
    output = work / device
    output.mkdir()
    request = {"input": [str(p) for p in images], "output": str(output), "result": str(output / "result.json"),
               "config": config, "options": {"fastdvdnet": True, "video": True}, "fps": "24"}
    atomic_json(output / "request.json", request)
    child_env = dict(env)
    if device == "cpu":
        child_env["CUDA_VISIBLE_DEVICES"] = ""
    with (output / "smoke.log").open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-B", str(ROOT / "farm/post_entry.py"), str(output / "request.json")],
                       env=child_env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=180, check=True)
    report = read_json(output / "result.json")
    assert report["verified"] and report["decoded_frames"] == 3 and report["lossless_rgb_hash_match"]
    assert report["fastdvdnet_sigma"] == 10
    for p in output.glob("frame.*.png"):
        with Image.open(p) as im:
            im.load()
            assert im.size == (67, 65)
    reports[device] = report
    print(device, "inference / 3 frames / verified FFV1 completed", flush=True)
assert [sha256(p) for p in images] == hashes
atomic_json(work / "verification.json", {"python": sys.version, "torch": torch.__version__, "cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0), "caps": caps, "input_unchanged": True,
            "installation": read_json(ROOT / "tools/fastdvdnet/installation.json"), "reports": reports})
print("Verified:", work / "verification.json", flush=True)

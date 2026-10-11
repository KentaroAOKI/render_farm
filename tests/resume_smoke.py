"""Real Maya stop/resume check against the isolated 2027 fixture farm."""
import os
from pathlib import Path
import subprocess
import sys
import time
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.assets import enqueue
from farm.common import atomic_json, python_command, read_json, runtime_env, sha256, worker_dir
from farm.process import terminate_owned
from farm.store import Store

root = ROOT / "tests/runtime/farm2027"
store = Store(root)
scene = next(iter(store.read()["scenes"]))
jid = enqueue(store, scene, {"end": 10, "layers": ["LayerA"], "device": "CPU", "post": {"video": False, "fastdvdnet": False}})
command = python_command(sys.executable, ROOT / "farm_cli.py", ["--root", root, "worker", "--once"])
proc = subprocess.Popen(command, env=runtime_env(root), cwd=root)
before = None
try:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        job = store.read()["jobs"][jid]
        if job.get("work"):
            work = root / job["work"]
            frames = read_json(work / "frames.json", {})
            if frames:
                atomic_json(worker_dir(root) / "worker_control.json", {"stop": True})
                break
        if job["state"] in {"failed", "complete"}:
            raise AssertionError((job["state"], job.get("error")))
        time.sleep(.1)
    proc.wait(timeout=120)
    job = store.read()["jobs"][jid]
    assert job["state"] == "paused", (job["state"], job.get("error"))
    before = read_json(work / "frames.json")
    assert 0 < len(before) < 10
    store.action(jid, "resume", job["revision"])
    proc = subprocess.Popen(command, env=runtime_env(root), cwd=root)
    proc.wait(timeout=300)
    job = store.read()["jobs"][jid]
    assert job["state"] == "complete", (job["state"], job.get("error"))
    after = read_json(work / "frames.json")
    assert len(after) == 10
    assert all(after[key] == record for key, record in before.items()), "Finished frames were re-rendered"
    atomic_json(ROOT / "tests/runtime/resume_test_result.json", {"passed": True, "job": jid, "preserved_frames": len(before), "final_frames": len(after)})
    print("PASS: real Maya stop/resume preserved", len(before), "completed frames and finished all 10")
finally:
    if proc.poll() is None:
        terminate_owned(proc)

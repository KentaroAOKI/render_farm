"""Real late processing: video-only and FastDVDnet, preserving source and queue state."""
from pathlib import Path
import sys
import shutil
import uuid
import os
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import atomic_json, read_json, sha256, runtime_env, FileLock
from farm.store import Store
from farm.worker import worker_config
from farm.late_post import run_late_postprocess

root = ROOT / "tests/runtime" / ("late_post_" + uuid.uuid4().hex[:8])
os.environ.update(runtime_env(root))
store = Store(root)
publication = "results/test-scene"
source = root / publication
source.mkdir(parents=True)
settings = {"start": 1, "end": 3, "step": 1, "fps": "24", "post": {"video": False, "fastdvdnet": False}}
job = {"id": "test-job", "scene_id": "test-scene", "state": "complete", "publication": publication, "settings": settings, "signature": "unchanged", "revision": 1}
frames = {}
for i in range(1, 4):
    file = source / "raw" / ("beauty.%06d.png" % i)
    file.parent.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "tests/runtime/fastdvdnet_6a654aed/input" / file.name, file)
    frames[str(i)] = {"layer": "Layer", "camera": "Camera", "frame": i,
                      "files": [{"path": file.relative_to(source).as_posix(), "pass": "beauty", "sha256": sha256(file)}]}
atomic_json(source / "manifest.json", {"verified": True, "job": job, "frames": frames})
with store.transaction() as state:
    state["jobs"][job["id"]] = job
before = {p.relative_to(source).as_posix(): sha256(p) for p in source.rglob("*") if p.is_file()}
events = []
config = worker_config(ROOT)
video = run_late_postprocess(store, job["id"], {"video": True}, config, progress=events.append)
denoise = run_late_postprocess(store, job["id"], {"video": True, "fastdvdnet": True}, config, progress=events.append)
assert video["publication"] != denoise["publication"]
for report in (video, denoise):
    manifest = read_json(root / report["publication"] / "manifest.json")
    assert manifest["verified"] and manifest["groups"][0]["decoded_frames"] == 3
    assert manifest["groups"][0]["lossless_rgb_hash_match"]
    for file in manifest["files"]:
        assert sha256(root / report["publication"] / file["path"]) == file["sha256"]
assert before == {name: sha256(source / name) for name in before}
assert not any(len(p.name) in {16, 24, 32, 64} and all(c in '0123456789abcdef' for c in p.name)
               for p in source.rglob('*') if p.is_dir())
current = store.read()["jobs"][job["id"]]
for key in ("state", "settings", "signature", "revision", "publication"):
    assert current[key] == job[key]
assert len(current["postprocess_runs"]) == 2
assert any(e.get("total") == 3 for e in events)
try:
    run_late_postprocess(store, job["id"], {"video": True}, config, cancelled=lambda: True)
except InterruptedError:
    pass
else:
    raise AssertionError("Cancellation not respected")
with FileLock(root / "data/late_post_test-job.lock", timeout=0):
    try:
        run_late_postprocess(store, job["id"], {"video": True}, config)
    except TimeoutError:
        pass
    else:
        raise AssertionError("Duplicate run allowed")
atomic_json(root / "verification.json", {"source_unchanged": True, "job_unchanged": True, "video": video,
            "fastdvdnet_video": denoise, "cancelled": True, "duplicate_blocked": True})
print("Late post-processing verified:", root / "verification.json", flush=True)

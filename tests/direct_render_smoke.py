"""Render an isolated saved_* fixture directly from its registered folder."""
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import atomic_json, read_json, runtime_env, worker_dir, python_command, sha256
from farm.store import Store
base = Path(__file__).resolve().parent / "runtime"
root = (base / sys.argv[1]).resolve()
assert root.is_relative_to(base.resolve()) and root.name.startswith("saved_")
store = Store(root)
state = store.read()
job = next(iter(state["jobs"].values()))
assert job["state"] == "pending" and job["source_mode"] == "direct"
project = store.project_path(state["projects"][job["source"]["project_id"]])
scene = project / job["source"]["relative"]
before = sha256(scene)
os.environ.update(runtime_env(root))
atomic_json(worker_dir(root) / "config.json", {"enabled_versions": [job["maya_version"]]})
result = subprocess.run(python_command(sys.executable, Path(__file__).resolve().parents[1] / "farm_cli.py",
    ["--root", root, "worker", "--once"]), env=runtime_env(root), cwd=root, timeout=240)
after = store.read()["jobs"][job["id"]]
assert result.returncode == 0 and after["state"] == "complete", after.get("error")
work = root / after["work"]
request = read_json(work / "request.json")
assert Path(request["project"]).resolve() == project.resolve()
assert Path(request["scene"]).resolve() == scene.resolve()
assert "sources" not in request and not (work / "sources").exists()
assert not (root / "data/blobs").exists() and not (root / "data/snapshots").exists()
assert before == sha256(scene)
atomic_json(root / "direct_render_verification.json", {"passed": True, "frames": after["completed"],
    "source_unchanged": True, "project_copies": False, "publication": after["publication"]})
print("Direct project render completed", root, after["completed"])

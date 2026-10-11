"""Real Maya: package old reference/texture roots and render after USB relocation."""
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.assets import probe, refresh_scene_list
from farm.common import atomic_json, read_json, runtime_env, sha256, worker_dir, python_command
from farm.package import export_package
from farm.store import Store

version = sys.argv[1] if len(sys.argv) > 1 else "2027"
base = Path(__file__).parent / "runtime" / ("package_remap_" + version + "_" + uuid.uuid4().hex[:8])
farm = base / "source_farm"
project = base / "RemapProject"
os.environ.update(runtime_env(farm))
probe(farm, project, "", version, mode="make_fixture",
      payload={"external": str(project / "assets"), "aov": True})
old_root = "E:/MissingOldPC/RemapProject"
for path in project.rglob("*.ma"):
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(str(project.resolve()).replace("\\", "/"), old_root), encoding="utf-8")
for path in project.rglob("*"):
    if path.is_file():
        os.utime(path, (time.time() - 10, time.time() - 10))
scene = project / "scenes/test.ma"
before = sha256(scene)
source = Store(farm)
pid = source.add_project(project, version)
refresh_scene_list(source, pid)
ids = list(source.read()["scenes"])
original = base / "USB_export"
report = export_package(source, original, ids)
assert report["ready"] and report["warnings"], report
assert before == sha256(scene)
moved = base / "USB_moved"
assert original.resolve().is_relative_to(base.resolve()) and moved.resolve().is_relative_to(base.resolve())
original.rename(moved)
offline = base / "source_offline"
assert project.resolve().is_relative_to(base.resolve()) and offline.resolve().is_relative_to(base.resolve())
project.rename(offline)
atomic_json(worker_dir(moved) / "config.json", {"enabled_versions": [version]})
result = subprocess.run(python_command(sys.executable, moved / "farm_cli.py", ["--root", moved, "worker", "--once"]),
                        cwd=moved, env=runtime_env(moved), timeout=240)
jobs = list(Store(moved).read()["jobs"].values())
assert result.returncode == 0 and all(j["state"] == "complete" for j in jobs), [(j["state"], j.get("error")) for j in jobs]
for job in jobs:
    work = moved / job["work"]
    assert all(path.is_file() for path in (work / "raw").iterdir())
    for record in read_json(work / "frames.json").values():
        for file in record["files"]:
            assert Path(file["path"]).parent.as_posix() == "raw"
            assert (work / file["path"]).name.endswith(f'.{record["frame"]:06d}' + Path(file["path"]).suffix)
atomic_json(base / "verification.json", {"passed": True, "version": version, "source_unchanged": True,
    "original_sources_unavailable": True, "warnings": report["warnings"], "relocated": True,
    "publications": [j["publication"] for j in jobs]})
print("Mapped USB package rendered", base)

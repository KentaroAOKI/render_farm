"""Real Maya package relocation test. Uses only tests/runtime fixtures."""
import os
from pathlib import Path
import subprocess
import sys
import uuid
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.store import Store
from farm.assets import refresh_project
from farm.package import export_package
from farm.common import atomic_json, worker_dir, runtime_env, python_command

runtime = ROOT / "tests/runtime"
source = Store(runtime / "portable_source_farm")
suffix = uuid.uuid4().hex[:8]
original = runtime / ("USB_export_" + suffix)
moved = runtime / ("USB_移動後_" + suffix)
report = export_package(source, original, list(source.read()["scenes"]), {"enabled_versions": ["2027"]})
assert original.resolve().is_relative_to(runtime.resolve()) and moved.resolve().is_relative_to(runtime.resolve())
original.rename(moved)
hidden = []
try:
    for name in ("portable_project", "external_assets"):
        path = runtime / name
        offline = runtime / (name + "_offline_" + suffix)
        assert path.resolve().is_relative_to(runtime.resolve()) and offline.resolve().is_relative_to(runtime.resolve())
        path.rename(offline)
        hidden.append((offline, path))
    target = Store(moved)
    previous = {sid: scene["revision"] for sid, scene in target.read()["scenes"].items()}
    for pid in target.read()["projects"]:
        refresh_project(target, pid, {"enabled_versions": ["2027"]})
    assert {sid: scene["revision"] for sid, scene in target.read()["scenes"].items()} == previous, "Relocation changed content identity"
    assert all(job["state"] == "pending" for job in target.read()["jobs"].values())
    atomic_json(worker_dir(moved) / "config.json", {"enabled_versions": ["2027"]})
    proc = subprocess.run(python_command(sys.executable, moved / "farm_cli.py", ["--root", moved, "worker", "--once"]),
                          env=runtime_env(moved), cwd=moved, timeout=600)
    jobs = list(target.read()["jobs"].values())
    assert proc.returncode == 0 and all(j["state"] == "complete" for j in jobs), [(j["state"], j.get("error")) for j in jobs]
    atomic_json(runtime / "portable_test_result.json", {"passed": True, "root": str(moved), "farm_id": target.read()["farm_id"],
                "jobs": [j["id"] for j in jobs], "original_sources_unavailable": True, "path_relocated": True})
    print("PASS: relocated package rendered with original project and external reference folders unavailable")
finally:
    for offline, path in reversed(hidden):
        assert offline.resolve().is_relative_to(runtime.resolve()) and path.resolve().is_relative_to(runtime.resolve())
        offline.rename(path)

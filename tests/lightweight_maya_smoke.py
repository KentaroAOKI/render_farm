"""Real Maya inspection only; creates original fixtures under tests/runtime."""
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env, sha256, atomic_json, read_json
from farm.assets import probe, refresh_project, ensure_detailed, enqueue
from farm.store import Store

version = sys.argv[1]
root = Path(__file__).resolve().parent / "runtime" / ("lightweight_" + version + "_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
project, external = root / "project", root / "external"
marker = root / "script_executed.txt"
probe(root, project, "", version, mode="make_fixture", payload={"external": str(external),
      "aov": True, "layers": True, "reference_camera": True, "script_marker": str(marker)})
assert not marker.exists()
before = sha256(project / "scenes/test.ma")
time.sleep(2.1)
store = Store(root)
pid = store.add_project(project, version)
start = time.monotonic()
refresh_project(store, pid)
light_seconds = time.monotonic() - start
scene = next(iter(store.read()["scenes"].values()))
assert scene["state"] != "error", scene.get("error")
sid = scene["id"]
assert scene["metadata"]["inspection"] == "lightweight"
# The date catalog launches no Maya; exercise the optional lightweight Maya probe separately.
light = probe(root, project, project / "scenes/test.ma", version, payload={"lightweight": True})
assert light["inspection"] == "lightweight"
assert light["references"] and not any(v["loaded"] for v in light["references"].values())
assert not any("asset:" in c["name"] for c in light["cameras"])
assert not marker.exists()
start = time.monotonic()
ensure_detailed(store, [sid])
full_seconds = time.monotonic() - start
full = store.read()["scenes"][sid]["metadata"]
assert full["inspection"] == "full"
assert any(v["loaded"] for v in full["references"].values())
assert any("asset:" in c["name"] for c in full["cameras"])
assert any("texture.png" in dep for dep in full["dependencies"])
assert len(full["layers"]) == 2 and any(a["name"] == "N" for a in full["aovs"])
assert not marker.exists()
assert before == sha256(project / "scenes/test.ma")
jid = enqueue(store, sid, prepared_revision=store.read()["scenes"][sid]["revision"])
job = store.read()["jobs"][jid]
snapshot = read_json(root / job["snapshot"])
assert snapshot["metadata"]["inspection"] == "full"
assert any(path.endswith("texture.png") for path in snapshot["files"])
report = {"version": version, "passed": True, "light_seconds": light_seconds, "full_seconds": full_seconds,
          "reference_loaded_light": False, "reference_loaded_full": True, "script_executed": marker.exists()}
atomic_json(root / "verification.json", report)
print(report)
print(root / "verification.json")

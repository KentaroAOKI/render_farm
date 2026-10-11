"""Enqueue cached catalog metadata without probing or cataloging source files again."""
import os
from pathlib import Path
import sys
import time
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env, read_json, atomic_json
from farm.assets import probe, refresh_scene_list, enqueue
from farm.store import Store
version = sys.argv[1]
root = Path(__file__).resolve().parent / "runtime" / ("saved_" + version + "_" + uuid.uuid4().hex[:8])
os.environ.update(runtime_env(root))
project, external = root / "project", root / "external"
probe(root, project, "", version, mode="make_fixture", payload={"external": str(external), "aov": True})
time.sleep(2.1)
store = Store(root)
pid = store.add_project(project, version)
refresh_scene_list(store, pid)
scene = next(iter(store.read()["scenes"].values()))
assert scene["metadata"]["enqueue_ready"]
assert scene["metadata"]["references"] and any(r["loaded"] for r in scene["metadata"]["references"].values())
with patch("farm.assets.ensure_detailed", side_effect=AssertionError("Unexpected scene inspection")), \
     patch("farm.assets.latest_scenes", side_effect=AssertionError("Unexpected file update check")), \
     patch("farm.assets.create_snapshot", side_effect=AssertionError("Unexpected snapshot")), \
     patch("farm.assets.inventory", side_effect=AssertionError("Unexpected asset inventory")):
    jid = enqueue(store, scene["id"])
job = store.read()["jobs"][jid]
assert job["snapshot"] is None and job["source_mode"] == "direct"
assert not (root / "data/blobs").exists() and not (root / "data/snapshots").exists()
result = probe(root, project, project / job["source"]["relative"], version,
    mode="verify_package", payload={"metadata": job["source"]["metadata"]})
assert result["verified"]
atomic_json(root / "verification.json", {"passed": True, "job": jid, "scene_inspection_at_enqueue": False,
                                         "direct_references_verified": True})
print("Saved catalog direct enqueue and reference loading passed", version, root)

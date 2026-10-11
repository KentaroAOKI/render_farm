"""Verify saved ranges with script execution disabled and GUI range selection."""
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env, atomic_json, sha256
from farm.assets import probe, validate_settings, refresh_scene_list
from farm.store import Store

version = sys.argv[1]
root = Path(__file__).resolve().parent / "runtime" / ("ranges_" + version + "_" + uuid.uuid4().hex[:8])
os.environ.update(runtime_env(root))
project = root / "project"
probe(root, project, "", version, mode="make_fixture", payload={
    "playback_range": [11, 21, 1], "animation_range": [-10, 50], "time_unit": "pal",
    "second_camera": True, "render_step": 3, "script_marker": str(root / "must_not_execute")})
scene = project / "scenes/test.ma"
original = sha256(scene)
time.sleep(2.1)
store = Store(root)
pid = store.add_project(project, version)
refresh_scene_list(store, pid)
record = next(iter(store.read()["scenes"].values()))
info = record["metadata"]
assert record["metadata_source_stamp"] == record["source_stamp"]
def unexpected_probe(*args, **kwargs):
    raise AssertionError("Unchanged scene was inspected again")
refresh_scene_list(store, pid, inspect_fn=unexpected_probe)
assert next(iter(store.read()["scenes"].values()))["metadata"] == info
assert info["inspection"] == "catalog" and info["references_loaded"] is False
# Job settings still require the separate full render inspection.
catalog_info = info
info = probe(root, project, scene, version)
assert info["ranges"] == catalog_info["ranges"]
assert info["ranges"]["playback"] == {"start": 11, "end": 21, "step": 1}, info["ranges"]
assert info["ranges"]["animation"]["start"] == -10 and info["ranges"]["animation"]["end"] == 50
assert info["ranges"]["render"]["start"] == 1 and info["ranges"]["render"]["end"] == 2
assert info["ranges"]["render"]["step"] == 3
assert info["settings"]["start"] == 11 and info["settings"]["end"] == 21
assert info["time_unit"] == "pal" and info["settings"]["fps"] == "25"
assert info["resolution"]["width"] == 64 and info["resolution"]["height"] == 64
assert len(info["non_default_cameras"]) == 2
assert all(c["renderable"] and not c["default"] for c in info["non_default_cameras"])
assert len([c for c in info["cameras"] if c["default"]]) == 4
assert not (root / "must_not_execute").exists() and original == sha256(scene)
assert validate_settings({}, info)["start"] == 11
assert validate_settings({"range_source": "render"}, info)["end"] == 2
try:
    validate_settings({"start": 1.5}, info)
except ValueError:
    pass
else:
    raise AssertionError("Fractional frames were silently rounded")
from PySide6 import QtWidgets
from farm.gui import JobDialog
app = QtWidgets.QApplication([])
dialog = JobDialog({"name": "test", "maya_version": version, "metadata": info}, {}, None)
assert dialog.values()["start"] == 11 and dialog.values()["range_source"] == "playback"
dialog.range_source.setCurrentIndex(dialog.range_source.findData("animation"))
assert dialog.values()["start"] == -10 and dialog.values()["end"] == 50
assert dialog.values()["range_source"] == "animation"
dialog.fields["start"].setValue(5)
assert dialog.values()["range_source"] == "manual"
dialog.range_source.setCurrentIndex(dialog.range_source.findData("render"))
assert dialog.values()["start"] == 1 and dialog.values()["end"] == 2
assert dialog.values()["step"] == 3
assert dialog.values()["cameras"] == info["settings"]["cameras"]
dialog.show()
app.processEvents()
dialog.grab().save(str(root / "settings.png"))
dialog.close()
atomic_json(root / "verification.json", info)
print("Saved ranges and GUI selection passed", version, root)

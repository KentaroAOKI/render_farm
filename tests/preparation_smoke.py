"""Check that the job dialog sees full reference metadata, not lightweight results."""
import os
from pathlib import Path
import sys
import time
import uuid
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env
root = Path(__file__).resolve().parent / "runtime" / ("preparation_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
from PySide6 import QtWidgets
from farm import gui
from farm.assets import refresh_project, ensure_detailed
from test_core import metadata, stable_write

app = QtWidgets.QApplication([])
window = gui.Dashboard(root)
window.scan_timer.stop()
window.refresh_timer.stop()
project = root / "project"
stable_write(project / "scenes/test.ma", "fixture")
pid = window.store.add_project(project, "2027")

def inspect(*args, payload=None, **kwargs):
    info = metadata()
    light = payload["lightweight"]
    info["inspection"] = "lightweight" if light else "full"
    if not light:
        info["cameras"].append({"name": "asset:ReferenceCamera"})
    return info

refresh_project(window.store, pid, inspect_fn=inspect)
sid = next(iter(window.store.read()["scenes"]))
ensure_detailed(window.store, [sid], inspect_fn=inspect)
window.refresh()
window.scenes.selectRow(0)
dialogs = []

class CheckedDialog:
    def __init__(self, scene, settings, parent):
        dialogs.append(scene["metadata"])
        assert parent.preparing_scenes
        assert scene["metadata"]["inspection"] == "full"
    def exec(self):
        return QtWidgets.QDialog.Rejected

with patch("farm.assets.ensure_detailed", side_effect=AssertionError("Enqueue must use saved information")), patch.object(gui, "JobDialog", CheckedDialog):
    window.enqueue_selected()
    deadline = time.monotonic() + 10
    while window.tasks and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
assert not window.preparing_scenes
assert len(dialogs) == 1 and any(c["name"] == "asset:ReferenceCamera" for c in dialogs[0]["cameras"])
assert not window.store.read()["jobs"]
assert window.scene_page.action_buttons["選択シーンを投入"].isEnabled()
window.close()
print("Preparation smoke passed: saved metadata opens dialog without Maya inspection, cancel leaves no job")

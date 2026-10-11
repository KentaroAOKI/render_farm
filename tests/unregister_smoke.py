"""GUI unregistration uses resolved paths to distinguish same-name projects."""
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env
root = Path(__file__).resolve().parent / "runtime" / ("unregister_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
from PySide6 import QtWidgets
from farm import gui
from farm.assets import refresh_project, enqueue, ensure_detailed
from test_core import stable_write, inspector

app = QtWidgets.QApplication([])
window = gui.Dashboard(root)
window.scan_timer.stop()
window.refresh_timer.stop()
projects = [root / "original/SameName", root / "copy/SameName"]
pids = []
for project in projects:
    stable_write(project / "scenes/test.ma", "fixture")
    pid = window.store.add_project(project, "2027")
    refresh_project(window.store, pid, inspect_fn=inspector)
    pids.append(pid)
scene = next(s for s in window.store.read()["scenes"].values() if s["project_id"] == pids[0])
ensure_detailed(window.store, [scene["id"]], inspect_fn=inspector)
jid = enqueue(window.store, scene["id"], prepared_revision=window.store.read()["scenes"][scene["id"]]["revision"])
window.refresh()
assert window.scenes.rowCount() == 2

labels_seen = []
def choose(parent, title, prompt, labels, *args):
    labels_seen.extend(labels)
    window.scan()  # A modal dialog may dispatch the auto-scan timer.
    assert not window.scanning
    return labels[0], True

with patch.object(gui.W.QInputDialog, "getItem", side_effect=choose), \
     patch.object(gui.W.QMessageBox, "question", return_value=gui.W.QMessageBox.Yes):
    window.remove_project()
assert len(set(labels_seen)) == 2
assert str(projects[0]) in labels_seen[0] and str(projects[1]) in labels_seen[1]
state = window.store.read()
assert pids[0] not in state["projects"] and pids[1] in state["projects"]
assert state["jobs"][jid]["state"] == "deleted"
assert window.scenes.rowCount() == 1
assert window.jobs.rowCount() == 0
window.history.setChecked(True)
window.refresh()
assert window.jobs.rowCount() == 1
assert not window.registering_project
assert window.scene_page.action_buttons["プロジェクト登録を解除"].isEnabled()
assert all((project / "scenes/test.ma").is_file() for project in projects)
window.close()
print("Unregister GUI passed: distinct paths, cancellation, retained history, files untouched")

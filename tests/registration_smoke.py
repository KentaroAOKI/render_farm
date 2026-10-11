"""Regression for registration during an active scan (Maya mayapy/PySide6)."""
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env

root = Path(__file__).resolve().parent / "runtime" / ("registration_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
from PySide6 import QtWidgets
from farm import gui

app = QtWidgets.QApplication([])
window = gui.Dashboard(root)
window.scan_timer.stop()
window.refresh_timer.stop()
first, second = root / "Project_A", root / "Project_B"
first.mkdir()
second.mkdir()
pid = window.store.add_project(first, "2027")
started, release = threading.Event(), threading.Event()
calls = []

def held_scan(store, project_id, config, progress):
    calls.append(project_id)
    progress({"project": "Project_A", "phase": "test scan", "path": str(first), "completed": 3, "total": 10})
    started.set()
    if not release.wait(10):
        raise TimeoutError("test release timed out")
    return 0

def drain():
    deadline = time.monotonic() + 10
    while (window.scanning or window.tasks) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)

with patch.object(gui, "refresh_project", held_scan):
    try:
        window.scan()
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert started.is_set()
        app.processEvents()
        assert not window.scene_page.action_buttons["プロジェクトを登録"].isEnabled()
        assert not window.scene_page.action_buttons["更新を確認"].isEnabled()
        before = window.store.read()["projects"]
        before_event = window.scan_event.copy()
        with patch.object(gui.W.QFileDialog, "getExistingDirectory", return_value=str(second)) as dialog, \
             patch.object(gui.W.QInputDialog, "getItem", return_value=("2027", True)):
            window.add_project()
        after = window.store.read()["projects"]
        dialog_calls = dialog.call_count
        window.scan()
        assert calls == [pid], "duplicate scan started"
        assert before_event == window.scan_event, "active progress was replaced"
    finally:
        release.set()
        drain()
        window.close()

assert before == after, "registration changed project records during active scan"
assert dialog_calls == 0, "registration dialog opened during active scan"
assert window.scene_page.action_buttons["プロジェクトを登録"].isEnabled()
assert window.scene_page.action_buttons["更新を確認"].isEnabled()

# Qt timers can run inside a modal file dialog: they must not start a scan.
def cancel_dialog(*args, **kwargs):
    window.scan()
    assert not window.scanning
    return ""

with patch.object(gui.W.QFileDialog, "getExistingDirectory", side_effect=cancel_dialog), \
     patch.object(gui, "refresh_project") as unexpected_scan:
    window.add_project()
    app.processEvents()
    unexpected_scan.assert_not_called()
assert not window.registering_project
assert window.scene_page.action_buttons["プロジェクトを登録"].isEnabled()

# Registration preserves the list until the explicit update action.
new_calls = []
def fresh_scan(store, project_id, config, progress):
    assert not window.registering_project
    new_calls.append(project_id)
    progress({"project": "same display name", "phase": "same phase", "completed": 0, "total": 1})
    return 0

with patch.object(gui.W.QFileDialog, "getExistingDirectory", return_value=str(second)), \
     patch.object(gui.W.QInputDialog, "getItem", return_value=("2027", True)), \
     patch.object(gui, "refresh_project", fresh_scan), \
     patch.object(window, "scan_progress", wraps=window.scan_progress) as progress_display:
    window.add_project()
    assert not new_calls
    window.scan()
    drain()
    events = [call.args[0] for call in progress_display.call_args_list]
    assert len({event["project_id"] for event in events}) == 2
    assert {event["project_root"] for event in events} == {str(first), str(second)}
assert len(new_calls) == 2 and pid in new_calls
assert len(window.store.read()["projects"]) == 2
assert window.scene_page.action_buttons["プロジェクトを登録"].isEnabled()
print("Registration smoke passed: active scan protected, dialog timer blocked, next registration succeeds")

"""Saved list on startup, explicit scan only, and persisted scene settings."""
import os
from pathlib import Path
import sys
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env
from farm.store import Store
from farm.assets import refresh_scene_list
root = Path(__file__).resolve().parent / "runtime" / ("manual_" + uuid.uuid4().hex[:8])
os.environ.update(runtime_env(root))
project = root / "project"
(project / "scenes").mkdir(parents=True)
scene = project / "scenes/test.ma"
scene.write_text("fixture")
os.utime(scene, (time.time() - 10, time.time() - 10))
store = Store(root)
store.add_project(project, "2027")
from PySide6 import QtWidgets
from farm import gui
app = QtWidgets.QApplication([])
info = {"inspection": "catalog", "ranges": {"playback": {"start": 11, "end": 21, "step": 1},
        "animation": {"start": 1, "end": 50, "step": 1}, "render": {"start": 1, "end": 10, "step": 2}}, "time_unit": "pal",
        "settings": {"fps": "25"}, "warnings": ["未知のノード（情報取得は継続）: Example"],
        "resolution": {"width": 1920, "height": 1080}, "non_default_cameras": [{"name": "Camera", "renderable": True}]}
def scan(*args, **kwargs):
    return refresh_scene_list(*args, inspect_fn=lambda *a, **k: info, **kwargs)
with patch.object(gui, "refresh_project", side_effect=scan) as inspect:
    window = gui.Dashboard(root)
    assert not window.scan_timer.isActive()
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    inspect.assert_not_called()
    assert window.scenes.rowCount() == 0
    window.scene_page.action_buttons["更新を確認"].click()
    deadline = time.monotonic() + 10
    while window.tasks and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not window.tasks and window.scenes.rowCount() == 1
    assert window.scenes.item(0, 11).text() == "11 ～ 21"
    assert window.scenes.item(0, 12).text() == "1 ～ 50"
    assert window.scenes.item(0, 13).text() == "1 ～ 10"
    assert window.scenes.item(0, 14).text() == "2"
    assert window.scenes.item(0, 15).text() == "pal / 25"
    assert window.scenes.item(0, 16).text() == "1920 × 1080"
    assert "renderable=True" in window.scenes.item(0, 17).text()
    assert "Example" in window.scenes.item(0, 18).text()
    window.show()
    app.processEvents()
    window.scenes.scrollToItem(window.scenes.item(0, 18))
    app.processEvents()
    window.grab().save(str(root / "scene_information.png"))
    assert next(iter(store.read()["scenes"].values()))["metadata"] == info
    assert inspect.call_count == 1
    window.scene_page.action_buttons["シーン情報を強制再取得"].click()
    assert not window.scene_page.action_buttons["シーン情報を強制再取得"].isEnabled()
    deadline = time.monotonic() + 10
    while window.tasks and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not window.tasks and inspect.call_count == 2
    assert inspect.call_args.kwargs["force"] is True
    assert window.scene_page.action_buttons["シーン情報を強制再取得"].isEnabled()
    window.close()
    reopened = gui.Dashboard(root)
    assert reopened.scenes.rowCount() == 1
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert inspect.call_count == 2
    reopened.close()
print("Manual scan GUI passed: no startup scan, explicit update saves metadata, restart uses saved list")
print(root / "scene_information.png")

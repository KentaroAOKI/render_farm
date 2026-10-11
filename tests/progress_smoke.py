"""Run with Maya mayapy; all fixtures stay under tests/runtime."""
import os
from pathlib import Path
import sys
import time
import uuid
import re
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env, sha256

root = Path(__file__).resolve().parent / "runtime" / ("progress_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
from PySide6 import QtWidgets
from farm import gui
from farm.assets import inventory, refresh_project
from test_core import inspector, stable_write

project = root / "日本語 project"
stable_write(project / "scenes" / "shot.ma", "test scene")
asset = project / "texture.bin"
asset.write_bytes(b"a" * (9 * 1024 * 1024))
os.utime(asset, (time.time() - 10, time.time() - 10))
events = []
files = inventory(project, progress=events.append)
assert files["texture.bin"]["hash"] == sha256(asset)
assert any(0 < e.get("file_bytes", 0) < asset.stat().st_size for e in events)
assert events[-1]["completed"] == events[-1]["total"] == 2

app = QtWidgets.QApplication([])
window = gui.Dashboard(root)
window.scan_timer.stop()
window.refresh_timer.stop()
window.store.add_project(project, "2027")

def slow_inspect(*args, **kwargs):
    time.sleep(3.5)
    return inspector(*args, **kwargs)

def scan(*args, **kwargs):
    emit = kwargs.get("progress")
    if emit:
        emit({"project": "日本語 project", "phase": "シーンの更新日時を確認中（ハッシュ計算なし）", "path": str(project), "total": None})
    time.sleep(3.5)
    return refresh_project(*args, inspect_fn=slow_inspect, **kwargs)

seen = []
window.show()
with patch.object(gui, "refresh_project", scan):
    window.scan()
    deadline = time.monotonic() + 20
    captured = False
    while (window.scanning or window.tasks) and time.monotonic() < deadline:
        app.processEvents()
        label = window.scan_label.text()
        seen.append(label)
        if "シーンの更新日時" in label and re.search(r"工程 [1-9]\d*秒", label) and not captured:
            window.grab().save(str(root / "progress.png"))
            captured = True
        time.sleep(.02)
assert not window.scanning and not window.tasks, "background scan did not finish"
assert captured, "elapsed display did not advance while Maya inspection waited"
assert any("シーンの更新日時" in label for label in seen)
assert "結果を反映" in window.scan_label.text()
assert window.scenes.rowCount() == 1
assert "shot.ma" in window.scenes.item(0, 2).text()
assert window.scan_bar.value() == window.scan_bar.maximum()

def broken_scan(*args, **kwargs):
    raise OSError("simulated share unavailable")

with patch.object(gui, "refresh_project", broken_scan):
    window.scan()
    deadline = time.monotonic() + 5
    while (window.scanning or window.tasks) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.02)
assert not window.scanning
assert "simulated share unavailable" in window.scan_label.text()
assert "エラー" in window.scan_bar.format()
assert window.scene_page.action_buttons["プロジェクトを登録"].isEnabled()
assert window.scene_page.action_buttons["更新を確認"].isEnabled()
window.close()
print("Progress smoke passed: byte progress, live timer, scene, completion, failure")
print(root / "progress.png")

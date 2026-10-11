"""Check package progress widgets and timer using an isolated dashboard."""
import os
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env, atomic_json
from unittest.mock import patch
root = Path(__file__).parent / "runtime" / ("package_gui_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtCore, QtWidgets
from farm.gui import Dashboard
app = QtWidgets.QApplication([])
window = Dashboard(root)
window.show_text = lambda *args: None
window.packing = True
window.pack_started = time.monotonic() - 61
window.pack_destination = str(root / "USB")
window.pack_button.setEnabled(False)
window.pack_extras_button.setEnabled(False)
warnings = [{"scene": "shot", "message": "旧パスを読み替え"}]
mappings = [{"scene": "shot", "source": "E:/old/project", "resolved": "//server/project",
             "target": "projects/id/project"}]
window.pack_progress({"phase": "保存先へ素材をコピー中", "completed": 2, "total": 4,
                      "warnings": warnings, "mappings": mappings,
                      "file_bytes": 1048576, "file_size": 2097152,
                      "copied_bytes": 3145728, "total_bytes": 6291456, "path": "日本語の素材.exr"})
assert "1分01秒" in window.pack_label.text() and "MiB" in window.pack_label.text()
assert window.pack_bar.maximum() == 4 and window.pack_bar.value() == 2
assert window.pack_warnings.rowCount() == window.pack_mappings.rowCount() == 1
assert window.pack_mappings.item(0, 1).text() == "E:/old/project"
assert window.pack_mappings.item(0, 3).text() == "projects/id/project"
before = window.pack_label.text()
QtCore.QTimer.singleShot(2300, app.quit)
app.exec()
assert window.pack_label.text() != before
window.pack_failed("test failure")
assert not window.packing and window.pack_button.isEnabled()
assert "梱包失敗" in window.pack_label.text()
assert window.pack_warnings.rowCount() == window.pack_mappings.rowCount() == 1
window.packing = True
window.pack_complete({"jobs": ["a", "b"]})
assert not window.packing and "梱包完了" in window.pack_label.text()
assert window.pack_bar.value() == window.pack_bar.maximum() == 2
saved = root / "USB"
atomic_json(saved / "package_status.json", {"ready": True})
atomic_json(saved / "package_warnings.json", warnings)
atomic_json(saved / "package_mappings.json", mappings)
window.update_pack_reports({"warnings": [], "mappings": []})
with patch("farm.gui.W.QFileDialog.getExistingDirectory", return_value=str(saved)):
    window.load_package_reports()
assert window.pack_warnings.rowCount() == window.pack_mappings.rowCount() == 1
assert "保存済み梱包：完了" in window.pack_label.text()
older = root / "old_package"
atomic_json(older / "package_status.json", {"ready": True, "warnings": warnings})
atomic_json(older / "data/state.json", {"projects": {"id": {"name": "Older project",
    "portable": {"mappings": [["E:/old/project", "projects/id/project"]]}}}})
with patch("farm.gui.W.QFileDialog.getExistingDirectory", return_value=str(older)):
    window.load_package_reports()
assert window.pack_mappings.rowCount() == 1 and window.pack_warnings.rowCount() == 1
assert window.pack_mappings.item(0, 1).text() == "E:/old/project"
window.close()
print("Package progress GUI passed", root)

"""Verify worker progress display in an isolated dashboard."""
import os
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env
from farm.store import Store
root = Path(__file__).parent / "runtime" / ("worker_gui_" + uuid.uuid4().hex)
os.environ.update(runtime_env(root))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtWidgets
from farm.gui import Dashboard
store = Store(root)
with store.transaction() as state:
    state["jobs"]["job"] = {"id": "job", "name": "test scene", "maya_version": "2027", "state": "rendering",
        "order": 1, "owner": "TestPC", "completed": 4, "total": 10, "elapsed": 200,
        "frame": 5, "layer": "LayerA", "camera": "CameraShape", "frame_started": time.time() - 30,
        "loading_seconds": 60, "max_frame_seconds": 90}
store.worker_status("TestPC", state="working", job="job", capabilities={"2027": {}})
app = QtWidgets.QApplication([])
window = Dashboard(root)
window.refresh()
assert window.workers.item(0, 4).text() == "test scene"
assert window.workers.item(0, 7).text() == "4 / 10"
assert window.workers.item(0, 8).text() == "40.0%"
assert window.workers.item(0, 9).text() == "5"
assert window.workers.item(0, 13).text() == "00:01:00"
assert window.workers.item(0, 14).text() == "00:01:30"
with store.transaction() as state:
    state["jobs"]["job"].update(state="postprocessing", completed=10)
window.refresh()
assert window.workers.item(0, 6).text() == "後処理中"
assert window.workers.item(0, 8).text() == "100.0%"
assert window.workers.item(0, 9).text() == ""
store.worker_status("TestPC", state="idle", job=None)
window.refresh()
assert window.workers.item(0, 7).text() == "—"
assert window.workers.item(0, 4).text() == ""
store.worker_status("TestPC", state="diagnosing")
window.refresh()
assert window.workers.item(0, 6).text() == "環境診断中"
window.close()
print("Worker progress GUI passed", root)

from pathlib import Path
import os
import sys
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env, atomic_json
from farm.store import Store
root = Path(__file__).parent / "runtime" / ("late_post_gui_" + uuid.uuid4().hex[:8])
os.environ.update(runtime_env(root))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtWidgets as W
from farm.gui import Dashboard
store = Store(root)
job = {"id": "job", "scene_id": "scene", "name": "test scene", "maya_version": "2027", "state": "complete",
       "order": 1, "publication": "results/source", "settings": {"fps": "24"}}
with store.transaction() as state:
    state["jobs"]["job"] = job
atomic_json(root / "results/source/manifest.json", {"verified": True, "job": job,
            "frames": {"one": {"layer": "L", "camera": "C", "frame": 1, "files": [{"pass": "beauty", "path": "raw/beauty.000001.png"}]}}})
app = W.QApplication([])
window = Dashboard(root)
window.jobs.selectRow(0)
captured = {}
def fake_run(store, jid, options, config, **kwargs):
    captured.update(job_id=jid, options=options, selection=kwargs["selection"], fps=kwargs["fps"])
    kwargs["progress"]({"phase": "動画化", "completed": 1, "total": 1, "group": 1, "groups": 1})
    assert window.late_post_bar.value() == 100
    return {"publication": "results/scene/postprocess/job/output"}
def fake_background(fn, label, **kwargs):
    result = fn(kwargs["progress"])
    kwargs["callback"](result)
    kwargs["finished"]()
with patch.object(W.QDialog, "exec", return_value=W.QDialog.Accepted), patch("farm.gui.run_late_postprocess", fake_run), patch.object(window, "background", fake_background):
    window.late_post_selected()
assert captured == {"job_id": "job", "options": {"video": True, "fastdvdnet": False}, "selection": [("L", "C")], "fps": "24"}
assert window.late_post_button.isEnabled() and not window.late_post_stop.isEnabled()
assert "完了" in window.late_post_label.text()
with store.transaction() as state:
    state["jobs"]["job"]["postprocess_runs"] = [{"state": "complete", "publication": "results/scene/postprocess/job/output"}]
with patch("farm.gui.os.startfile") as open_folder:
    window.open_post_result()
    assert open_folder.call_args.args[0] == str(root.resolve() / "results/scene/postprocess/job/output")
window.close()
print("Late post GUI passed", root)

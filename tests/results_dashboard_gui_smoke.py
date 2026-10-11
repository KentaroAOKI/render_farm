import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import runtime_env
root = ROOT / 'tests/runtime' / ('dashboard_gui_' + uuid.uuid4().hex[:8])
os.environ.update(runtime_env(root))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6 import QtWidgets as W
from farm.gui import Dashboard
app = W.QApplication([])
window = Dashboard(root)
opened = []
window.open_results_dashboard = lambda: opened.append(True)
def fake(store, config, progress, cancelled):
    assert not cancelled()
    progress({'phase': '動画変換中', 'completed': 1, 'total': 2})
    return {'html': 'results/dashboard.html', 'media': 'results/dashboard/date', 'videos': 2, 'scenes': 2}
with patch('farm.results_dashboard.build_dashboard', fake):
    window.preview_button.click()
    deadline = time.monotonic() + 10
    while window.tasks and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    app.processEvents()
    assert not window.tasks
    assert window.preview_button.isEnabled()
    assert not window.preview_stop.isEnabled()
    assert window.preview_bar.value() == 100
    assert '2動画' in window.preview_label.text()
    assert opened
window.close()
print('Results dashboard GUI smoke passed')

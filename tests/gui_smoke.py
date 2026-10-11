import os
import sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import runtime_env

root = Path(__file__).resolve().parent / "runtime" / "farm2027"
os.environ.update(runtime_env(root))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from farm.gui import Dashboard
app = QApplication([])
app.setFont(QFont("Yu Gothic UI", 10))
window = Dashboard(root)
window.scan_timer.stop()
window.refresh_timer.stop()
window.scan = lambda: None
window.show()
app.processEvents()
assert window.tabs.count() == 5
assert window.scenes.rowCount() >= 1
window.grab().save(str(Path(__file__).resolve().parent / "runtime" / "dashboard.png"))
window.tabs.setCurrentIndex(4)
app.processEvents()
window.grab().save(str(Path(__file__).resolve().parent / "runtime" / "settings.png"))
print("GUI smoke: five tabs and scene table rendered")
window.close()

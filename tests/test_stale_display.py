import os
import sys
import time
import unittest
import uuid
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from farm.gui import Dashboard
from farm.store import Store


class StaleDisplayTests(unittest.TestCase):
    def test_stale_render_is_not_counted_as_live(self):
        app = QApplication.instance() or QApplication([])
        root = Path(__file__).parent / 'runtime' / ('stale_display_' + uuid.uuid4().hex[:8])
        store = Store(root)
        with store.transaction() as state:
            state['projects']['project'] = {'name': 'Example', 'maya_version': '2027'}
            state['scenes']['scene'] = {'id': 'scene', 'project_id': 'project', 'name': 'scene',
                'relative': 'scenes/scene.mb', 'revision': 'v1', 'state': 'queued', 'maya_version': '2027',
                'metadata': {}, 'source_mtime': 0}
            state['jobs']['job'] = {'id': 'job', 'name': 'scene', 'scene_id': 'scene',
                'scene_revision': 'v1', 'state': 'rendering', 'owner': 'Example-PC_123456789abc',
                'maya_version': '2027', 'order': 1, 'created': 1, 'updated': time.time() - 120,
                'completed': 2, 'total': 10, 'restarts': {}, 'error': None}
            state['workers']['Example-PC_123456789abc'] = {'state': 'working', 'job': 'job',
                'updated': time.time() - 120, 'capabilities': {'2027': {}}}
        window = Dashboard(root)
        try:
            self.assertIn('応答中の実行 0', window.summary.text())
            self.assertIn('更新なし 1', window.summary.text())
            self.assertIn('更新なし', window.jobs.item(0, 2).text())
            self.assertIn('更新なし', window.scenes.item(0, 5).text())
            self.assertIn('更新なし', window.workers.item(0, 1).text())
            self.assertEqual(store.read()['jobs']['job']['state'], 'rendering')
        finally:
            window.close()

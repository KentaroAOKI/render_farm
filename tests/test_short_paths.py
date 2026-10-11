import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.common import atomic_json, read_json
from farm.post import process_outputs
from farm.store import Store


class ShortPathTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / 'runtime' / ('paths_' + uuid.uuid4().hex[:8])
        self.store = Store(self.root)

    def test_new_job_id_is_short_and_stored_consistently(self):
        with self.store.transaction() as state:
            state['scenes']['scene'] = {'id': 'scene', 'revision': 'v1', 'state': 'unrendered',
                'name': 'scenes/c073/c073', 'maya_version': '2027', 'metadata': {'plugins': {}}}
        settings = {'layers': ['layer'], 'cameras': ['camera'], 'start': 1, 'end': 1, 'step': 1}
        jid = self.store.enqueue_record('scene', 'v1', None, settings)
        self.assertEqual(len(jid), 16)
        self.assertEqual(self.store.read()['jobs'][jid]['id'], jid)

    def test_old_32_character_job_path_can_postprocess_below_win32_limit(self):
        # This reproduces a production-sized layout that reached 260 characters when
        # progress.json used a 32-character temporary suffix.
        job_id = 'a' * 32
        work = self.root / 'workers' / 'RENDER-NODE-0001_123456789abc' / 'jobs' / job_id
        raw = work / 'raw' / 'beauty.000001.png'
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b'fixture')
        atomic_json(work / 'frames.json', {'one': {'layer': 'defaultRenderLayer',
            'camera': '|c048_Cam_Rotate|c048_Cam_RotateShape', 'frame': 1,
            'files': [{'pass': 'beauty', 'path': 'raw/beauty.000001.png'}]}})
        job = {'signature': 'signature', 'settings': {'post': {'video': True, 'fastdvdnet': False}, 'fps': '24'}}
        def launch(command, **kwargs):
            request = read_json(next((work / 'processed').rglob('request.json')))
            output = Path(request['output'])
            self.assertEqual(output.name, 'g01')
            self.assertLess(len(str(output / ('progress.json.' + 'a' * 16 + '.tmp'))), 260)
            atomic_json(output / 'progress.json', {'phase': 'done'})
            atomic_json(output / 'result.json', {'verified': True, 'frames': 1})
            (output / 'video.mkv').write_bytes(b'verified fixture')
            return Mock(poll=Mock(return_value=0), returncode=0)
        with patch('farm.post.check_dependencies', return_value={'ffmpeg': True, 'fastdvdnet': False}), \
             patch('farm.post.subprocess.Popen', side_effect=launch), \
             patch('farm.post.ProcessGroup'):
            process_outputs(self.root, work, job, {'ffmpeg': 'fixture'}, lambda: False)
        manifest = read_json(work / 'post_manifest.json')
        self.assertEqual(manifest['groups'][0]['layer'], 'defaultRenderLayer')
        self.assertEqual(manifest['groups'][0]['camera'], '|c048_Cam_Rotate|c048_Cam_RotateShape')
        self.assertEqual(len(manifest['files']), 1)
        self.assertTrue(manifest['files'][0]['path'].endswith('/g01/video.mkv'))

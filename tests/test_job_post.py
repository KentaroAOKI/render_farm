import sys
import unittest
import uuid
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.store import Store, ACTIVE
from farm.common import atomic_json, read_json
from farm.post import process_outputs


class JobPostTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(__file__).parent / 'runtime' / ('job_post_' + uuid.uuid4().hex))
        self.work = self.store.root / 'workers/test/jobs/job'
        self.frames = {'frame': {'signature': 'original', 'files': []}}
        atomic_json(self.work / 'frames.json', self.frames)
        with self.store.transaction() as state:
            state['jobs']['job'] = {'id': 'job', 'state': 'failed', 'revision': 2,
                'signature': 'original', 'scene_id': 'scene', 'scene_revision': 'v1',
                'work': 'workers/test/jobs/job', 'error': 'torch timeout', 'events': [],
                'restarts': {}, 'settings': {'post': {'video': True, 'fastdvdnet': True}}}
            state['scenes']['scene'] = {'revision': 'v1', 'state': 'unrendered'}

    def test_disable_denoise_preserves_frames_and_retry(self):
        job = self.store.update_job_post('job', {'video': True, 'fastdvdnet': False}, 2)
        self.assertEqual(job['signature'], 'original')
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['revision'], 3)
        self.assertEqual(read_json(self.work / 'frames.json'), self.frames)
        self.store.action('job', 'retry', 3)
        self.assertEqual(self.store.read()['jobs']['job']['state'], 'pending')

    def test_stale_revision_and_active_jobs_rejected(self):
        with self.assertRaises(ValueError):
            self.store.update_job_post('job', {'video': True, 'fastdvdnet': False}, 1)
        for status in ACTIVE | {'complete', 'deleted'}:
            with self.store.transaction() as state:
                state['jobs']['job']['state'] = status
            with self.assertRaises(ValueError):
                self.store.update_job_post('job', {'video': True, 'fastdvdnet': False}, 2)
        self.assertTrue(self.store.read()['jobs']['job']['settings']['post']['fastdvdnet'])

    def test_disable_all_clears_old_post_manifest_without_dependencies(self):
        job = self.store.update_job_post('job', {'video': False, 'fastdvdnet': False}, 2)
        atomic_json(self.work / 'post_manifest.json', {'signature': 'original', 'files': [{'path': 'old.mkv'}]})
        process_outputs(self.store.root, self.work, job, {}, lambda: False)
        self.assertEqual(read_json(self.work / 'post_manifest.json')['files'], [])
        self.assertEqual(read_json(self.work / 'frames.json'), self.frames)

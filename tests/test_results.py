import sys
import time
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.store import Store
from farm.common import atomic_json, sha256, read_json, digest, FileLock
from farm.results import migrate_results, result_path
from farm.worker import publication


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(__file__).parent / 'runtime' / ('results_' + uuid.uuid4().hex))
        self.job = {'id': 'job-id', 'name': 'scenes/c097/c097', 'scene_id': 'scene-id',
                    'scene_revision': 'revision', 'signature': 'signature', 'token': 'owner',
                    'state': 'publishing', 'revision': 0, 'settings': {
                        'layers': ['layer'], 'cameras': ['camera'], 'start': 1, 'end': 1, 'step': 1}}
        with self.store.transaction() as state:
            state['jobs'][self.job['id']] = self.job.copy()
            state['scenes']['scene-id'] = {'name': 'scenes/c097/c097', 'revision': 'revision'}

    def payload(self, base):
        image = base / 'raw/beauty.000001.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'verified payload')
        return {digest(('layer', 'camera', 1))[:24]: {'layer': 'layer', 'camera': 'camera', 'frame': 1,
                    'signature': 'signature', 'files': [{'path': 'raw/beauty.000001.png', 'sha256': sha256(image)}]}}

    def test_publication_raw_direct_and_duplicate_does_not_overwrite(self):
        work = self.store.root / 'workers/test/jobs/job'
        frames = self.payload(work)
        atomic_json(work / 'frames.json', frames)
        first = publication(self.store, self.job, work)
        self.assertEqual(first, 'results/c097')
        self.assertEqual(self.store.read()['jobs']['job-id']['completed'], 1)
        self.assertEqual(self.store.read()['jobs']['job-id']['total'], 1)
        self.assertTrue((self.store.root / first / 'raw/beauty.000001.png').exists())
        with self.store.transaction() as state:
            state['jobs']['job-id'].update(state='publishing', token='owner')
        second = publication(self.store, self.job, work)
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith('results/c097_'))
        self.assertTrue((self.store.root / first / 'raw/beauty.000001.png').exists())
        self.assertEqual(self.store.read()['scenes']['scene-id']['latest'], second)

    def legacy(self):
        old = 'results/scene-id/versions/version-id'
        base = self.store.root / old
        frames = self.payload(base)
        atomic_json(base / 'manifest.json', {'verified': True, 'job': self.job, 'frames': frames, 'post': {}})
        with self.store.transaction() as state:
            state['jobs']['job-id'].update(state='complete', publication=old)
            state['scenes']['scene-id']['latest'] = old
        return old, base

    def test_legacy_migration_verified_idempotent_updates_references(self):
        old, base = self.legacy()
        report = migrate_results(self.store)
        self.assertEqual(report, [{'old': old, 'new': 'results/c097'}])
        self.assertFalse(base.exists())
        self.assertFalse((self.store.root / 'results/scene-id').exists())
        self.assertTrue((self.store.root / 'results/c097/raw/beauty.000001.png').exists())
        self.assertEqual(self.store.read()['jobs']['job-id']['publication'], 'results/c097')
        self.assertEqual(migrate_results(self.store), [])

    def test_damaged_result_is_not_moved(self):
        old, base = self.legacy()
        (base / 'raw/beauty.000001.png').write_bytes(b'damaged')
        with self.assertRaises(ValueError):
            migrate_results(self.store)
        self.assertTrue(base.exists())
        self.assertEqual(self.store.read()['jobs']['job-id']['publication'], old)

    def test_recovers_rename_before_state_commit(self):
        old, base = self.legacy()
        atomic_json(self.store.root / 'data/results_migration.json', {'moves': {old: 'results/c097'}})
        base.rename(self.store.root / 'results/c097')
        migrate_results(self.store)
        self.assertEqual(self.store.read()['jobs']['job-id']['publication'], 'results/c097')

    def test_cached_worker_processed_directories_are_normalized(self):
        old, base = self.legacy()
        path = 'processed/' + 'a' * 32 + '/' + 'b' * 16 + '/video.mkv'
        video = base / path
        video.parent.mkdir(parents=True)
        video.write_bytes(b'verified video')
        manifest = read_json(base / 'manifest.json')
        manifest['post'] = {'files': [{'path': path, 'sha256': sha256(video)}]}
        atomic_json(base / 'manifest.json', manifest)
        migrate_results(self.store)
        published = self.store.root / 'results/c097'
        updated = read_json(published / 'manifest.json')
        file = updated['post']['files'][0]
        self.assertEqual(file['path'], 'processed/出力_01/video.mkv')
        self.assertEqual(sha256(published / file['path']), file['sha256'])
        self.assertFalse((published / 'processed' / ('a' * 32)).exists())

    def test_migration_waits_for_active_postprocessing_lease(self):
        old, base = self.legacy()
        with FileLock(self.store.root / 'data/late_post_job-id.lock', timeout=0):
            self.assertEqual(migrate_results(self.store), [])
            self.assertTrue(base.exists())
        self.assertEqual(len(migrate_results(self.store)), 1)

    def late_legacy(self):
        self.legacy()
        migrate_results(self.store)
        old = 'results/scene-id/postprocess/job-id/run-id'
        base = self.store.root / old
        folder = digest(('layer', '|camera|cameraShape'))[:16]
        video = base / folder / 'video.mkv'
        video.parent.mkdir(parents=True)
        video.write_bytes(b'verified video')
        atomic_json(base / 'manifest.json', {'verified': True, 'job_id': 'job-id', 'source': 'results/c097',
                    'groups': [{'layer': 'layer', 'camera': '|camera|cameraShape'}],
                    'files': [{'path': folder + '/video.mkv', 'sha256': sha256(video)}]})
        with self.store.transaction() as state:
            state['jobs']['job-id']['postprocess_runs'] = [{'id': 'run-id', 'state': 'complete',
                'publication': old, 'source': 'results/c097', 'created': 1700000000}]
        return old, base

    def test_late_video_migration_removes_all_id_directories(self):
        old, base = self.late_legacy()
        report = migrate_results(self.store)
        new = report[0]['new']
        self.assertTrue(new.startswith('results/c097/postprocess/2023'))
        manifest = read_json(self.store.root / new / 'manifest.json')
        file = manifest['files'][0]
        self.assertEqual(file['path'], '01_layer___camera_cameraShape/video.mkv')
        self.assertEqual(sha256(self.store.root / new / file['path']), file['sha256'])
        self.assertFalse((self.store.root / 'results/scene-id').exists())
        self.assertEqual(self.store.read()['jobs']['job-id']['postprocess_runs'][0]['publication'], new)
        self.assertEqual(migrate_results(self.store), [])

    def test_damaged_video_migration_preserves_old_path(self):
        old, base = self.late_legacy()
        next(base.rglob('video.mkv')).write_bytes(b'broken')
        with self.assertRaises(ValueError):
            migrate_results(self.store)
        self.assertTrue(base.exists())
        self.assertEqual(self.store.read()['jobs']['job-id']['postprocess_runs'][0]['publication'], old)

    def test_late_migration_recovers_move_before_state_save(self):
        old, base = self.late_legacy()
        target = 'results/c097/postprocess/recovered'
        (self.store.root / target).parent.mkdir(parents=True)
        atomic_json(self.store.root / 'data/results_migration.json', {'moves': {old: target}})
        base.rename(self.store.root / target)
        migrate_results(self.store)
        self.assertEqual(self.store.read()['jobs']['job-id']['postprocess_runs'][0]['publication'], target)


if __name__ == '__main__':
    unittest.main()

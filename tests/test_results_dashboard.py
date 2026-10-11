import sys
import unittest
import uuid
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.common import atomic_json, sha256, FileLock
from farm.store import Store
from farm.results_dashboard import collect_results, page, natural, build_dashboard, sort_saved_dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(__file__).parent / 'runtime' / ('dashboard_unit_' + uuid.uuid4().hex))

    def video(self, name, folder, job_id, late=False):
        base = self.store.root / 'results' / folder
        base.mkdir(parents=True)
        video = base / 'video.mkv'
        video.write_bytes(b'verified video')
        file = {'path': 'video.mkv', 'sha256': sha256(video)}
        job = {'id': job_id, 'scene_id': name, 'name': name}
        with self.store.transaction() as state:
            state['jobs'][job_id] = job
        atomic_json(base / 'manifest.json', {'verified': True, **({'job_id': job_id, 'files': [file]} if late
                    else {'job': job, 'post': {'files': [file]}})})

    def test_all_render_and_late_videos_natural_scene_order(self):
        self.video('c10', 'c10/processed', 'job10')
        self.video('c2', 'c2/processed', 'job2')
        self.video('c2', 'c2/postprocess/2026', 'job2', late=True)
        self.video('c1', 'dashboard/excluded', 'job1')
        state, videos, _ = collect_results(self.store)
        self.assertEqual([v['name'] for v in videos], ['c2', 'c2', 'c10'])

    def test_html_escaping_relative_media_and_offline_player(self):
        card = {'id': 'scene', 'name': '<script>日本語&', 'status': '<error>', 'completed': 1,
                'total': 2, 'owner': 'PC&', 'thumbnail': '日本語.jpg'}
        video = {'scene_id': 'scene', 'label': '<script>', 'browser': '0001.mp4', 'original': 'c2/video.mkv'}
        document = page({}, [card], [video], 'dashboard/日時/')
        self.assertNotIn('<script>', document)
        self.assertIn('&lt;script&gt;', document)
        self.assertIn('combined.mp4', document)
        self.assertIn('controls preload="none"', document)
        self.assertNotIn('https://', document)

    def test_cards_sorted_by_display_name_even_if_input_is_unsorted(self):
        cards = [{'id': name, 'name': name, 'status': 'complete', 'completed': 1, 'total': 1, 'owner': 'PC'}
                 for name in ['c10', 'c049_a', 'c2', 'c049']]
        document = page({}, cards, [], 'dashboard/date/')
        expected = ['c2', 'c10', 'c049', 'c049_a']
        import re
        self.assertEqual(re.findall(r'<h2>(c.*?)</h2>', document), expected)
        path = self.store.root / 'results/dashboard.html'
        path.parent.mkdir(parents=True)
        path.write_text(document, encoding='utf-8')
        self.assertEqual(sort_saved_dashboard(self.store), 4)
        self.assertEqual(path.read_text(encoding='utf-8'), document)

    def test_duplicate_run_is_rejected(self):
        ffmpeg = self.store.root / 'dummy.exe'
        ffmpeg.write_bytes(b'dummy')
        with FileLock(self.store.root / 'data/results_dashboard.lock', timeout=0):
            with self.assertRaises(TimeoutError):
                build_dashboard(self.store, {'ffmpeg': str(ffmpeg)})

    def test_external_video_has_scene_folder_name_and_no_queue_entry(self):
        path = self.store.root / 'results/c020 日本語/video.MP4'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'external movie')
        before = self.store.read()
        state, videos, _ = collect_results(self.store)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]['name'], 'c020 日本語')
        self.assertTrue(videos[0]['external'])
        self.assertEqual(state, before)
        self.assertEqual(self.store.read(), before)

    def test_managed_and_dashboard_movies_are_not_rediscovered(self):
        self.video('c2', 'c2/processed', 'job2')
        (self.store.root / 'results/c2/processed/video.mkv').write_bytes(b'changed')
        generated = self.store.root / 'results/dashboard/date/combined.mp4'
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b'generated')
        _, videos, _ = collect_results(self.store)
        self.assertEqual(len(videos), 1)
        self.assertNotEqual(videos[0]['sha256'], sha256(videos[0]['path']))

    def test_unverified_results_are_not_imported_as_external(self):
        path = self.store.root / 'results/incomplete/video.mp4'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'partial movie')
        atomic_json(path.parent / 'manifest.json', {'verified': False})
        self.assertEqual(collect_results(self.store)[1], [])

    def test_external_png_sequence_sorted_number_and_beauty_only(self):
        raw = self.store.root / 'results/c020/raw'
        raw.mkdir(parents=True)
        for name in ('beauty.000010.png', 'beauty.000002.png', 'diffuse.000002.png'):
            (raw / name).write_bytes(b'image')
        atomic_json(raw.parent / 'sequence.json', {'fps': '24000/1001'})
        _, videos, _ = collect_results(self.store)
        self.assertEqual(len(videos), 1)
        self.assertEqual([f['frame'] for f in videos[0]['sequence']], [2, 10])
        self.assertEqual(videos[0]['fps'], '24000/1001')

    def test_existing_movie_prevents_duplicate_raw_sequence(self):
        root = self.store.root / 'results/c020'
        root.mkdir(parents=True)
        (root / 'beauty.000001.exr').write_bytes(b'image')
        (root / 'video.mkv').write_bytes(b'movie')
        self.assertNotIn('sequence', collect_results(self.store)[1][0])

    def test_invalid_sequence_fps_is_rejected(self):
        root = self.store.root / 'results/c020'
        root.mkdir(parents=True)
        (root / 'beauty.000001.exr').write_bytes(b'image')
        with self.assertRaises(ValueError):
            collect_results(self.store, sequence_fps='0')

    def test_managed_beauty_sequence_retains_original_verification_hash(self):
        base = self.store.root / 'results/c020'
        image = base / 'raw/beauty.000001.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'verified')
        original = sha256(image)
        job = {'id': 'job', 'scene_id': 'scene', 'name': 'c020', 'publication': 'results/c020'}
        with self.store.transaction() as state:
            state['jobs']['job'] = job
        atomic_json(base / 'manifest.json', {'verified': True, 'job': job, 'frames': {'one': {
            'frame': 1, 'files': [{'pass': 'beauty', 'path': 'raw/beauty.000001.png', 'sha256': original}]}}})
        image.write_bytes(b'broken')
        _, videos, _ = collect_results(self.store)
        self.assertEqual(videos[0]['sequence'][0]['sha256'], original)
        self.assertNotEqual(original, sha256(image))

    def test_foreign_farm_manifest_uses_recorded_video_hash(self):
        base = self.store.root / 'results/c007'
        (base / 'processed').mkdir(parents=True)
        movie = base / 'processed/video.mkv'
        movie.write_bytes(b'foreign verified video')
        expected = sha256(movie)
        atomic_json(base / 'manifest.json', {'schema_version': 1, 'cut': 'c007', 'frame_count': 120,
                    'published_by': 'Other-PC', 'video': {'path': 'processed/video.mkv', 'sha256': expected},
                    'verification': {'lossless_rgb_hash_match': True, 'decoded_frames': 120}})
        with self.store.transaction() as state:
            state['scenes']['registered-scene'] = {'id': 'registered-scene', 'name': 'scenes/c007/c007'}
        movie.write_bytes(b'changed')
        state, videos, _ = collect_results(self.store)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]['sha256'], expected)
        self.assertEqual(videos[0]['scene_id'], 'registered-scene')
        self.assertEqual(videos[0]['source_frames'], 120)

    def test_unknown_external_metadata_does_not_exclude_images(self):
        base = self.store.root / 'results/c007'
        base.mkdir(parents=True)
        (base / 'frame.000001.png').write_bytes(b'image')
        atomic_json(base / 'manifest.json', {'schema_version': 1, 'cut': 'c007'})
        self.assertEqual(len(collect_results(self.store)[1]), 1)


if __name__ == '__main__':
    unittest.main()

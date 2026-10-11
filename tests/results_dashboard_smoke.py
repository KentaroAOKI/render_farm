"""Two real FFV1 inputs at different FPS -> captioned browser H.264 reel."""
from pathlib import Path
import sys
import os
import subprocess
import uuid
import json
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import atomic_json, sha256, runtime_env, command_flags, read_json
from farm.store import Store
from farm.results_dashboard import build_dashboard

root = ROOT / 'tests/runtime' / ('dashboard_smoke_' + uuid.uuid4().hex[:8])
store = Store(root)
os.environ.update(runtime_env(root))
ffmpeg = ROOT / 'bin/ffmpeg.exe'
originals = {}
for name, color, fps in [('c10', 'blue', 24), ('c2_日本語', 'red', 12)]:
    base = root / 'results' / name / 'postprocess/20260927/01_layer_camera'
    base.mkdir(parents=True)
    video = base / 'video.mkv'
    subprocess.run([str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:s=64x48:r={fps}',
                    '-frames:v', '3', '-c:v', 'ffv1', str(video)], check=True, env=runtime_env(root), **command_flags())
    job = {'id': 'job_' + name, 'scene_id': name, 'name': name, 'state': 'complete', 'created': 1, 'completed': 3, 'total': 3}
    with store.transaction() as state:
        state['jobs'][job['id']] = job
        state['scenes'][name] = {'id': name, 'name': name, 'state': 'complete'}
    atomic_json(base / 'manifest.json', {'verified': True, 'job_id': job['id'],
                'files': [{'path': 'video.mkv', 'sha256': sha256(video)}]})
    originals[video] = sha256(video)
before = store.read()
events = []
result = build_dashboard(store, {'ffmpeg': str(ffmpeg)}, progress=events.append)
assert result['videos'] == 2
assert store.read() == before
assert all(sha256(path) == value for path, value in originals.items())
reel = root / result['media'] / 'combined.mp4'
probe = subprocess.run([str(ROOT / 'bin/ffprobe.exe'), '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                        '-show_streams', '-of', 'json', str(reel)], capture_output=True, check=True, **command_flags())
stream = json.loads(probe.stdout)['streams'][0]
assert stream['codec_name'] == 'h264' and stream['pix_fmt'] == 'yuv420p'
assert (stream['width'], stream['height']) == (1920, 1080)
assert stream['avg_frame_rate'] == '24/1'
assert int(stream['nb_read_frames']) == 9, stream
manifest = read_json(root / result['media'] / 'manifest.json')
assert [v['name'] for v in manifest['videos']] == ['c2_日本語', 'c10']
assert (root / result['media'] / '0001.jpg').is_file()
saved = (root / result['html']).read_bytes()
try:
    build_dashboard(store, {'ffmpeg': str(ffmpeg)}, cancelled=lambda: True)
except InterruptedError:
    pass
else:
    raise AssertionError('Cancellation ignored')
assert (root / result['html']).read_bytes() == saved
atomic_json(root / 'verification.json', {'result': result, 'source_unchanged': True, 'state_unchanged': True,
            'codec': stream['codec_name'], 'frames': 9, 'fps': '24/1', 'cancel_preserved_html': True})
print(root / 'verification.json')

from pathlib import Path
import os
import sys
import uuid
import subprocess
import json
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import atomic_json, sha256, runtime_env, command_flags, read_json
from farm.store import Store
from farm.results_dashboard import build_dashboard
root = ROOT / 'tests/runtime' / ('external_sequences_' + uuid.uuid4().hex[:8])
store = Store(root)
os.environ.update(runtime_env(root))
ffmpeg = ROOT / 'bin/ffmpeg.exe'
originals = {}
for name, extension, fps, color in [("c002 日本語's", 'png', 12, 'red'), ('c010', 'exr', 24, 'blue')]:
    raw = root / 'results' / name / 'raw'
    raw.mkdir(parents=True)
    args = [str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:s=64x48:r={fps}', '-frames:v', '3']
    if extension == 'exr':
        args += ['-vf', 'format=gbrpf32le', '-c:v', 'exr']
    args += ['-start_number', '10', str(raw / ('beauty.%06d.' + extension))]
    subprocess.run(args, check=True, **command_flags())
    atomic_json(raw.parent / 'sequence.json', {'fps': fps})
    for image in raw.iterdir():
        originals[image] = sha256(image)
before = store.read()
result = build_dashboard(store, {'ffmpeg': str(ffmpeg)})
assert result['videos'] == 2 and result['scenes'] == 2
assert store.read() == before
assert all(sha256(path) == value for path, value in originals.items())
manifest = read_json(root / result['media'] / 'manifest.json')
assert [v['name'] for v in manifest['videos']] == ["c002 日本語's", 'c010']
probe = subprocess.run([str(ROOT / 'bin/ffprobe.exe'), '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                        '-show_entries', 'stream=nb_read_frames,codec_name', '-of', 'json',
                        str(root / result['media'] / 'combined.mp4')], check=True, capture_output=True, **command_flags())
stream = json.loads(probe.stdout)['streams'][0]
assert int(stream['nb_read_frames']) == 9, stream
assert stream['codec_name'] == 'h264'
atomic_json(root / 'verification.json', {'result': result, 'png_exr': True, 'frames': 9,
            'source_unchanged': True, 'state_unchanged': True, 'unicode_apostrophe': True})
print(root / 'verification.json')

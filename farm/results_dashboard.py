"""Offline HTML previews and a captioned H.264 reel of published result videos."""
import html
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import quote
from fractions import Fraction

from .common import FileLock, atomic_json, command_flags, inside, read_json, runtime_env, sha256, worker_dir
from .process import ProcessGroup
from .results import scene_name, vacant, readable

STATUS = {'unrendered': '未レンダリング', 'updated': '更新あり', 'queued': 'キュー投入済み', 'pending': '待機',
          'held': '保留', 'paused': '停止中', 'claimed': '取得済み', 'opening': '読み込み中', 'rendering': 'レンダリング中',
          'validating': '検証中', 'postprocessing': '後処理中', 'publishing': '公開中', 'restarting': '再起動中',
          'waiting_io': '接続待機', 'complete': '完了', 'failed': '失敗', 'error': 'エラー', 'missing': '元ファイルなし'}


def natural(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', str(value)))


def external_identity(state, folder):
    job = next((j for j in state['jobs'].values() if j.get('publication')
                and Path(j['publication']).parts[:2] == ('results', folder)), None)
    if job:
        return job['scene_id'], scene_name(state, job)
    matches = [sid for sid, scene in state['scenes'].items()
               if not scene.get('superseded') and scene_name(state, {'scene_id': sid, 'name': scene['name']}) == folder]
    return (matches[0], folder) if len(matches) == 1 else ('external:' + folder, folder)


def collect_results(store, sequence_fps='24'):
    state = store.read()
    videos, previews = [], {}
    excluded = []
    known_files = set()
    image_hashes = {}
    identities = {}
    def identity(folder):
        if folder not in identities:
            identities[folder] = external_identity(state, folder)
        return identities[folder]
    for path in sorted((store.root / 'results').rglob('manifest.json')):
        if 'dashboard' in path.relative_to(store.root / 'results').parts:
            continue
        manifest = read_json(path, {})
        foreign = manifest.get('video')
        check = manifest.get('verification', {})
        if (manifest.get('verified') is not False and isinstance(foreign, dict)
                and foreign.get('path') and foreign.get('sha256') and check.get('lossless_rgb_hash_match') is True):
            folder = path.relative_to(store.root / 'results').parts[0]
            sid, name = identity(folder)
            video = inside(path.parent, foreign['path'])
            known_files.add(str(video).casefold())
            videos.append({'scene_id': sid, 'name': name, 'path': video, 'sha256': foreign['sha256'],
                           'external': True, 'source_frames': manifest.get('frame_count', check.get('decoded_frames', 0)),
                           'owner': manifest.get('published_by', '—'),
                           'label': name + ' / 外部レンダリング結果',
                           'original': video.relative_to(store.root / 'results').as_posix()})
            continue
        if not manifest.get('verified'):
            if manifest.get('verified') is False or manifest.get('job') or manifest.get('job_id') or check:
                excluded.append(path.parent.resolve())
            continue
        job = state['jobs'].get(manifest.get('job_id') or manifest.get('job', {}).get('id'))
        if not job:
            continue
        sid = job['scene_id']
        name = scene_name(state, job)
        for frame in sorted(manifest.get('frames', {}).values(), key=lambda f: f['frame']):
            beauty = next((f for f in frame['files'] if f.get('pass') == 'beauty'), None)
            if beauty:
                image_hashes[str(inside(path.parent, beauty['path'])).casefold()] = beauty['sha256']
            if beauty and sid not in previews:
                previews[sid] = {'path': inside(path.parent, beauty['path']), 'sha256': beauty['sha256']}
        files = manifest.get('files', []) + manifest.get('post', {}).get('files', [])
        for file in files:
            if Path(file['path']).suffix.lower() not in {'.mkv', '.mp4', '.mov', '.avi'}:
                continue
            video = inside(path.parent, file['path'])
            known_files.add(str(video).casefold())
            videos.append({'scene_id': sid, 'name': name, 'path': video, 'sha256': file['sha256'],
                           'label': name + ' / ' + str(Path(file['path']).parent),
                           'original': video.relative_to(store.root / 'results').as_posix()})
    # External videos need no queue entry or Maya scene. Managed videos retain
    # their recorded hashes; never rediscover them using a new hash on failure.
    root = store.root / 'results'
    for candidate in sorted(root.rglob('*')):
        if candidate.suffix.lower() not in {'.mkv', '.mp4', '.mov', '.avi', '.webm', '.m4v'} or not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if relative.parts[0] == 'dashboard':
            continue
        video = inside(root, relative)
        if str(video).casefold() in known_files or any(video.is_relative_to(p) for p in excluded):
            continue
        folder = relative.parts[0] if len(relative.parts) > 1 else candidate.stem
        sid, name = identity(folder)
        videos.append({'scene_id': sid, 'name': name, 'path': video, 'sha256': sha256(video),
                       'label': name + ' / 外部動画 / ' + relative.as_posix(), 'original': relative.as_posix(),
                       'external': True})
    sequences = {}
    for candidate in sorted(root.rglob('*')):
        if candidate.suffix.lower() not in {'.png', '.exr'}:
            continue
        relative = candidate.relative_to(root)
        if relative.parts[0] == 'dashboard':
            continue
        folder = relative.parts[0] if len(relative.parts) > 1 else candidate.stem
        sid, _ = identity(folder)
        if any(v['scene_id'] == sid for v in videos):
            continue
        if not candidate.is_file():
            continue
        image = inside(root, relative)
        if any(image.is_relative_to(p) for p in excluded):
            continue
        match = re.fullmatch(r'(.*?)(-?\d+)(\.(?:png|exr))', candidate.name, re.I)
        if not match:
            continue
        key = (sid, folder, image.parent, match[1], match[3].lower())
        sequences.setdefault(key, []).append((int(match[2]), image))
    beauty_scenes = {key[0] for key in sequences if 'beauty' in (str(key[2]) + '/' + key[3]).casefold()}
    for (sid, folder, parent, prefix, extension), images in sequences.items():
        if sid in beauty_scenes and 'beauty' not in (str(parent) + '/' + prefix).casefold():
            continue
        images.sort(key=lambda item: item[0])
        if len({frame for frame, _ in images}) != len(images):
            raise ValueError('連番のフレーム番号が重複しています: ' + str(parent))
        options = read_json(root / folder / 'sequence.json', {})
        fps = str(options.get('fps', sequence_fps))
        if Fraction(fps) <= 0:
            raise ValueError('連番FPSは正の数にしてください: ' + folder)
        files = [{'path': path, 'frame': frame,
                  'sha256': image_hashes[str(path).casefold()] if str(path).casefold() in image_hashes else sha256(path)}
                 for frame, path in images]
        first = files[0]
        _, name = identity(folder)
        videos.append({'scene_id': sid, 'name': name, 'path': first['path'], 'sha256': first['sha256'],
                       'sequence': files, 'fps': fps, 'external': True,
                       'label': name + ' / 連番 ' + prefix + extension + ' / ' + str(len(files)) + '枚 / ' + fps + 'fps',
                       'original': first['path'].relative_to(root).as_posix()})
    videos.sort(key=lambda v: (natural(v['name']), natural(v['original'])))
    return state, videos, previews


def page(state, cards, videos, media_prefix):
    esc = html.escape
    def url(path):
        return quote(path, safe='/')
    articles = []
    for card in sorted(cards, key=lambda item: natural(item['name'])):
        sid = card['id']
        clips = [v for v in videos if v['scene_id'] == sid]
        thumbnail = (f'<img loading="lazy" src="{url(media_prefix + card["thumbnail"])}" alt="{esc(card["name"])}">'
                     if card.get('thumbnail') else '<div class="placeholder">サムネイルなし</div>')
        media = ''.join(f'<details><summary>{esc(v["label"])}</summary><video controls preload="none" '
                        f'src="{url(media_prefix + v["browser"])}"></video><p><a href="{url(v["original"])}">原本</a></p></details>' for v in clips)
        articles.append(f'<article>{thumbnail}<div class="body"><h2>{esc(card["name"])}</h2><p>{esc(card["status"])} '
                        f' · {card["completed"]}/{card["total"]} フレーム · 動画 {len(clips)}件</p>'
                        f'<p>{esc(card["owner"])}</p>{media}</div></article>')
    reel = (f'<video controls preload="metadata" src="{url(media_prefix + "combined.mp4")}"></video>'
            f'<p><a href="{url(media_prefix + "combined.mp4")}">連結動画を開く</a></p>' if videos else '<p>公開済みの動画はまだありません。</p>')
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Maya レンダー結果</title><style>*{box-sizing:border-box}body{margin:0;background:#0c1016;color:#edf3fb;'
            'font:14px system-ui,"Yu Gothic UI",sans-serif}header,.reel{padding:20px}h1{font-size:24px}h2{font-size:18px}'
            'main{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px;padding:20px}'
            'article,.reel{background:#151b24;border:1px solid #293445;border-radius:12px;overflow:hidden}'
            '.reel{margin:20px}.body{padding:14px}img,.placeholder{width:100%;aspect-ratio:16/9;object-fit:contain;background:#05080c}'
            '.placeholder{display:grid;place-items:center;color:#91a0b4}video{display:block;width:100%;max-width:1280px;background:#000}'
            'a{color:#79bfff}summary{cursor:pointer;padding:12px 0;overflow-wrap:anywhere}p{color:#91a0b4}</style></head><body>'
            f'<header><h1>Maya レンダー結果</h1><p>シーン {len(cards)}件 · 公開動画 {len(videos)}件 · '
            f'作成 {time.strftime("%Y-%m-%d %H:%M:%S")}（作成時点の状態）</p></header>'
            f'<section class="reel"><h2>全動画の連結映像 — シーン名テロップ付き</h2>{reel}</section>'
            '<main>' + ''.join(articles) + '</main></body></html>')


def sort_saved_dashboard(store):
    """Apply display-name order to an existing page without re-encoding media."""
    with FileLock(store.root / 'data/results_dashboard.lock', timeout=0):
        path = store.root / 'results/dashboard.html'
        document = path.read_text(encoding='utf-8')
        main = re.search(r'<main>(.*?)</main>', document, re.S)
        if not main:
            raise ValueError('結果HTMLのシーン一覧が見つかりません')
        cards = re.findall(r'<article>.*?</article>', main[1], re.S)
        if ''.join(cards) != main[1]:
            raise ValueError('結果HTMLの形式が一致しません')
        def key(card):
            title = re.search(r'<h2>(.*?)</h2>', card, re.S)
            if not title:
                raise ValueError('シーン名が見つかりません')
            return natural(html.unescape(title[1]))
        updated = document[:main.start(1)] + ''.join(sorted(cards, key=key)) + document[main.end(1):]
        temporary = path.with_name('.dashboard.html.tmp')
        temporary.write_text(updated, encoding='utf-8')
        temporary.replace(path)
        return len(cards)


def build_dashboard(store, config, progress=None, cancelled=None):
    def report(phase, completed=0, total=0):
        atomic_json(store.root / 'data/results_dashboard_progress.json',
                    {'phase': phase, 'completed': completed, 'total': total, 'updated': time.time()})
        if progress:
            progress({'phase': phase, 'completed': completed, 'total': total})
    def guard():
        if cancelled and cancelled():
            raise InterruptedError('HTML・連結動画の作成を停止しました')
    ffmpeg = config.get('ffmpeg', '')
    if not ffmpeg or not Path(ffmpeg).is_file():
        raise ValueError('FFmpegがありません。設定画面のFFmpegパスを確認してください')
    with FileLock(store.root / 'data/results_dashboard.lock', timeout=0):
        report('resultsの配置を確認中')
        state, videos, previews = collect_results(store, config.get('sequence_fps', '24'))
        stamp = time.strftime('%Y%m%d_%H%M%S')
        work = inside(store.root, vacant(store.root, worker_dir(store.root).relative_to(store.root).as_posix() + '/dashboard/' + stamp))
        output = work / 'output'
        output.mkdir(parents=True)
        log_path = work / 'ffmpeg.log'
        def command(args):
            guard()
            with log_path.open('ab') as log:
                proc = subprocess.Popen([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', *args], cwd=output,
                                        env=runtime_env(store.root, work), stdout=log, stderr=log, **command_flags())
                group = ProcessGroup(proc)
                try:
                    while proc.poll() is None:
                        guard()
                        time.sleep(.2)
                    if proc.returncode:
                        raise RuntimeError('FFmpeg処理に失敗しました。ログ: ' + str(log_path))
                finally:
                    group.stop()
        if videos:
            font = next((p for p in (Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
                                    for name in ('meiryo.ttc', 'YuGothM.ttc', 'msgothic.ttc')) if p.is_file()), None)
            if not font:
                raise ValueError('日本語テロップ用フォント（メイリオ等）が見つかりません')
            shutil.copyfile(font, output / 'font.ttc')
        for index, video in enumerate(videos, 1):
            report('動画変換・テロップ追加: ' + video['name'], index - 1, len(videos))
            guard()
            for source in video.get('sequence', [video]):
                if sha256(source['path']) != source['sha256']:
                    raise ValueError('入力画像・動画のハッシュが一致しません: ' + str(source['path']))
            (output / 'caption.txt').write_text(video['name'], encoding='utf-8')
            browser = str(index).zfill(4) + '.mp4'
            filters = ('scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,'
                       'setsar=1,fps=24,drawtext=fontfile=font.ttc:textfile=caption.txt:expansion=none:'
                       'fontsize=36:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=12:x=24:y=24')
            inputs = ['-i', str(video['path'])]
            if video.get('sequence'):
                sequence_list = output / 'sequence.txt'
                sequence_list.write_text(''.join("file '" + f['path'].as_posix().replace("'", "'\\''") + "'\n"
                                               for f in video['sequence']), encoding='utf-8')
                inputs = ['-r', video['fps'], '-f', 'concat', '-safe', '0', '-i', 'sequence.txt']
            command(['-n', *inputs, '-map', '0:v:0', '-an', '-vf', filters,
                     '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', browser])
            thumbnail = str(index).zfill(4) + '.jpg'
            command(['-n', '-i', browser, '-frames:v', '1', '-vf', 'scale=640:-2', thumbnail])
            video.update(browser=browser, thumbnail=thumbnail)
        if videos:
            report('全動画を連結中', len(videos), len(videos))
            (output / 'concat.txt').write_text(''.join("file '" + v['browser'] + "'\n" for v in videos), encoding='utf-8')
            command(['-n', '-f', 'concat', '-safe', '1', '-i', 'concat.txt', '-c', 'copy', '-movflags', '+faststart', 'combined.mp4'])
            report('連結動画を復号して検証中')
            command(['-i', 'combined.mp4', '-map', '0:v:0', '-f', 'null', '-'])
        cards = []
        scenes = dict(state['scenes'])
        for video in videos:
            if video['scene_id'] not in scenes and video.get('external'):
                scenes[video['scene_id']] = {'id': video['scene_id'], 'name': video['name'],
                                             'state': '外部連番' if video.get('sequence') else '外部動画'}
        for job in state['jobs'].values():
            if job['scene_id'] not in scenes and any(v['scene_id'] == job['scene_id'] for v in videos):
                scenes[job['scene_id']] = {'id': job['scene_id'], 'name': job.get('name', 'scene'), 'state': '履歴'}
        for scene in sorted(scenes.values(), key=lambda s: natural(scene_name(state, {'scene_id': s['id'], 'name': s['name']}))):
            if scene.get('superseded'):
                continue
            sid = scene['id']
            jobs = [j for j in state['jobs'].values() if j['scene_id'] == sid and j['state'] != 'deleted']
            job = max(jobs, key=lambda j: j.get('created', 0)) if jobs else {}
            card = {'id': sid, 'name': scene_name(state, {'scene_id': sid, 'name': scene['name']}),
                    'status': scene.get('state', '不明') if scene.get('state') in {'updated', 'missing', 'error'} else job.get('state', scene.get('state', '不明')), 'completed': job.get('completed', 0),
                    'total': job.get('total', 0), 'owner': job.get('owner') or '—'}
            clip = next((v for v in videos if v['scene_id'] == sid), None)
            card['status'] = STATUS.get(card['status'], card['status'])
            if clip:
                card['thumbnail'] = clip['thumbnail']
                if not jobs and clip.get('external'):
                    card['status'] = '外部連番' if clip.get('sequence') else '外部動画'
                    card['completed'] = card['total'] = int(clip.get('source_frames', 0))
                    card['owner'] = clip.get('owner', '—')
                if not jobs and clip.get('sequence'):
                    card['completed'] = card['total'] = len(clip['sequence'])
            elif sid in previews:
                image = previews[sid]
                report('サムネイルを作成中: ' + card['name'])
                if sha256(image['path']) != image['sha256']:
                    raise ValueError('サムネイル原本のハッシュが一致しません')
                name = 'scene_' + str(len(cards) + 1).zfill(4) + '.jpg'
                command(['-n', '-i', str(image['path']), '-frames:v', '1', '-vf', 'scale=640:-2', name])
                card['thumbnail'] = name
            cards.append(card)
        for video in videos:
            guard()
            for source in video.get('sequence', [video]):
                if sha256(source['path']) != source['sha256']:
                    raise ValueError('作成中に原本画像・動画が変更されました')
        for name in ('font.ttc', 'caption.txt', 'concat.txt', 'sequence.txt'):
            path = output / name
            if path.exists():
                path.unlink()
        relative = vacant(store.root, 'results/dashboard/' + stamp)
        prefix = relative.removeprefix('results/') + '/'
        document = page(state, cards, videos, prefix)
        atomic_json(output / 'manifest.json', {'verified': True, 'created': time.time(),
                    'videos': [{**{k: str(v) for k, v in item.items() if k != 'sequence'},
                                **({'sequence': [{k: str(v) for k, v in source.items()} for source in item['sequence']]}
                                   if item.get('sequence') else {})} for item in videos],
                    'files': [{'path': p.name, 'sha256': sha256(p)} for p in output.iterdir() if p.is_file()]})
        guard()
        destination = inside(store.root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        output.rename(destination)
        temporary = store.root / 'results/.dashboard.html.tmp'
        temporary.write_text(document, encoding='utf-8')
        temporary.replace(store.root / 'results/dashboard.html')
        report('完了', len(videos), len(videos))
        return {'html': 'results/dashboard.html', 'media': relative, 'videos': len(videos), 'scenes': len(cards)}

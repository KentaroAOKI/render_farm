"""Human-readable result directories; IDs remain in metadata only."""
import re
import time
from pathlib import Path
from contextlib import ExitStack

from .common import atomic_json, inside, read_json, sha256, FileLock, digest


def readable(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:100]
    if not name or name.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        name = 'scene_' + name
    return name


def scene_name(state, job):
    scene = state.get('scenes', {}).get(job['scene_id'], {})
    name = scene.get('logical') or scene.get('name') or job.get('name') or 'scene'
    return readable(Path(str(name).replace('\\', '/')).stem)


def vacant(root, relative):
    """Call under the state lock for render results."""
    candidate = relative
    if inside(root, candidate).exists():
        candidate = relative + '_' + time.strftime('%Y%m%d_%H%M%S')
    base, index = candidate, 2
    while inside(root, candidate).exists():
        candidate = base + '_' + str(index)
        index += 1
    return candidate


def result_path(store, state, job):
    return vacant(store.root, 'results/' + scene_name(state, job))


def normalize_post(directory, manifest):
    """Convert cached old-worker processing directories to readable names."""
    post = manifest.get('post', {})
    parents = {}
    for file in post.get('files', []):
        path = Path(file['path'])
        if len(path.parts) >= 4 and path.parts[0] == 'processed' and re.fullmatch('[0-9a-f]{32}', path.parts[1]):
            old_parent = path.parent.as_posix()
            if old_parent not in parents:
                parents[old_parent] = 'processed/出力_' + str(len(parents) + 1).zfill(2)
            new_path = parents[old_parent] + '/' + path.name
            check = inside(directory, file['path'])
            if not check.exists():
                check = inside(directory, new_path)
            if sha256(check) != file['sha256']:
                raise ValueError('後処理画像の移行検証に失敗しました')
            file['path'] = new_path
    for old, new in parents.items():
        source, target = inside(directory, old), inside(directory, new)
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            if not any(source.parent.iterdir()):
                source.parent.rmdir()
    if parents:
        atomic_json(directory / 'manifest.json', manifest)


def _migrate_render_results(store):
    """Move verified legacy publications, preserving files and job/history references."""
    moved = []
    if not any(job.get('publication', '').startswith('results/' + job['scene_id'] + '/versions/')
               for job in store.read()['jobs'].values() if job.get('publication')):
        return moved
    with FileLock(store.root / 'data/results_migration.lock'):
        with ExitStack() as leases, store.transaction() as state:
            journal_path = store.root / 'data/results_migration.json'
            journal = read_json(journal_path, {'moves': {}})
            for job in state['jobs'].values():
                old = job.get('publication')
                if not old or not old.startswith('results/' + job['scene_id'] + '/versions/'):
                    continue
                if any(run.get('state') == 'running' for run in job.get('postprocess_runs', [])):
                    continue
                try:
                    leases.enter_context(FileLock(store.root / 'data' / ('late_post_' + job['id'] + '.lock'), timeout=0))
                except TimeoutError:
                    continue
                source = inside(store.root, old)
                new = journal['moves'].get(old) or result_path(store, state, job)
                target = inside(store.root, new)
                check = source if source.exists() else target
                manifest = read_json(check / 'manifest.json', {})
                if not manifest.get('verified') or manifest.get('job', {}).get('id') != job['id']:
                    raise ValueError('移行する成果物の検証記録が一致しません: ' + old)
                for frame in manifest['frames'].values():
                    for file in frame['files']:
                        if sha256(inside(check, file['path'])) != file['sha256']:
                            raise ValueError('移行する画像のハッシュが一致しません: ' + file['path'])
                normalize_post(check, manifest)
                records = [file for frame in manifest['frames'].values() for file in frame['files']]
                records += manifest.get('post', {}).get('files', [])
                for file in records:
                    if sha256(inside(check, file['path'])) != file['sha256']:
                        raise ValueError('移行する成果物のハッシュが一致しません: ' + file['path'])
                journal['moves'][old] = new
                atomic_json(journal_path, journal)
                if source.exists():
                    source.rename(target)
                for other in state['jobs'].values():
                    if other.get('publication') == old:
                        other['publication'] = new
                    for run in other.get('postprocess_runs', []):
                        if run.get('source') == old:
                            run['source'] = new
                for scene in state['scenes'].values():
                    if scene.get('latest') == old:
                        scene['latest'] = new
                for archive in state.get('project_history', {}).values():
                    for scene in archive.get('scenes', {}).values():
                        if scene.get('latest') == old:
                            scene['latest'] = new
                atomic_json(target / 'latest.json', {'path': new})
                moved.append({'old': old, 'new': new})
    # Remove only empty legacy containers and their obsolete pointer file.
    for item in moved:
        parent = inside(store.root, item['old']).parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            pointer = parent.parent / 'latest.json'
            if pointer.exists():
                pointer.unlink()
            if not any(parent.parent.iterdir()):
                parent.parent.rmdir()
    return moved


def _migrate_late_results(store):
    moved = []
    def legacy(job, run):
        return (run.get('state') == 'complete' and run.get('publication', '').startswith(
            'results/' + job['scene_id'] + '/postprocess/' + job['id'] + '/'))
    if not any(legacy(job, run) for job in store.read()['jobs'].values()
               for run in job.get('postprocess_runs', [])):
        return moved
    with FileLock(store.root / 'data/results_migration.lock'):
        with ExitStack() as leases, store.transaction() as state:
            journal_path = store.root / 'data/results_migration.json'
            journal = read_json(journal_path, {'moves': {}})
            for job in state['jobs'].values():
                if not any(legacy(job, run) for run in job.get('postprocess_runs', [])):
                    continue
                try:
                    leases.enter_context(FileLock(store.root / 'data' / ('late_post_' + job['id'] + '.lock'), timeout=0))
                except TimeoutError:
                    continue
                for run in job['postprocess_runs']:
                    if not legacy(job, run):
                        continue
                    old = run['publication']
                    source = inside(store.root, old)
                    stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(run['created']))
                    new = journal['moves'].get(old) or vacant(store.root, job['publication'] + '/postprocess/' + stamp)
                    target = inside(store.root, new)
                    check = source if source.exists() else target
                    manifest = read_json(check / 'manifest.json', {})
                    if not manifest.get('verified') or manifest.get('job_id') != job['id']:
                        raise ValueError('動画の移行検証記録が一致しません: ' + old)
                    parents = {}
                    groups = manifest.get('groups', [])
                    for file in manifest['files']:
                        path = Path(file['path'])
                        if len(path.parts) > 1 and re.fullmatch('[0-9a-f]{16}', path.parts[0]):
                            parent = path.parts[0]
                            if parent not in parents:
                                index = len(parents)
                                match = next(((i, g) for i, g in enumerate(groups)
                                              if digest((g.get('layer'), g.get('camera')))[:16] == parent), None)
                                if match:
                                    index, group = match
                                else:
                                    group = groups[index] if index < len(groups) else {}
                                parents[parent] = (str(index + 1).zfill(2) + '_' + readable(group.get('layer', 'layer'))[:32]
                                                   + '__' + readable(group.get('camera', 'camera'))[:32])
                            new_path = '/'.join([parents[parent], *path.parts[1:]])
                        else:
                            new_path = file['path']
                        original = inside(check, file['path'])
                        if not original.exists():
                            original = inside(check, new_path)
                        if sha256(original) != file['sha256']:
                            raise ValueError('動画の移行ハッシュが一致しません: ' + file['path'])
                        file['path'] = new_path
                    # All files verified before any directory is renamed. Repeated
                    # execution also recovers a rename preceding manifest/state save.
                    for before, after in parents.items():
                        if inside(check, before).exists():
                            inside(check, before).rename(inside(check, after))
                    manifest['source'] = job['publication']
                    atomic_json(check / 'manifest.json', manifest)
                    journal['moves'][old] = new
                    atomic_json(journal_path, journal)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.exists():
                        source.rename(target)
                    run.update(publication=new, source=job['publication'])
                    if run.get('work'):
                        record = inside(store.root, run['work']) / 'run.json'
                        if record.exists():
                            atomic_json(record, run)
                    moved.append({'old': old, 'new': new})
    for item in moved:
        parent = inside(store.root, item['old']).parent
        boundary = store.root / 'results'
        while parent != boundary and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return moved


def migrate_results(store):
    return _migrate_render_results(store) + _migrate_late_results(store)


def bridge(root, pid):
    """Compatibility for an already running supervisor with cached legacy code."""
    import ctypes
    from .store import Store
    store = Store(root)
    handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
    if not handle:
        migrate_results(store)
        return
    try:
        while True:
            try:
                migrate_results(store)
            except Exception as exc:
                atomic_json(store.root / 'data/results_migration_error.json', {'error': str(exc)})
            if ctypes.windll.kernel32.WaitForSingleObject(handle, 2000) != 258:
                migrate_results(store)
                return
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)

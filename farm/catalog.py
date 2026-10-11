"""Scene-only catalog: no content reads, Maya startup, or material traversal."""
from pathlib import Path
import re
import time


def scene_key(relative):
    path = Path(relative)
    parents = [p for p in path.parts[:-1] if p.casefold() != "history"]
    stem = path.stem
    # Shot folders are authoritative; history names may contain dates and revision numbers.
    cut_folders = [index for index, name in enumerate(parents) if re.fullmatch(r"c\d+", name, re.I)]
    if cut_folders:
        parents = parents[:cut_folders[-1] + 1]
        stem = parents[-1]
    else:
        stem = re.sub(r"^\d{8}[_ -]+(?:\d+[_ -]+)?", "", stem)
        stem = re.sub(r"[_ -]+\d{8}(?:[_ -]+\d+)?$", "", stem)
        stem = re.sub(r"[_ -]+v\d+$", "", stem, flags=re.I)
    return "/".join([*parents, stem]).casefold()


def _scene_files(base, project):
    # Only the scenes root and each immediate shot folder are catalog sources.
    # Skip History before inspecting it; never enumerate nested directories.
    for path in base.iterdir():
        if path.name.startswith(".") or path.name.casefold() == "history":
            continue
        if path.is_dir():
            if path.is_symlink() or not path.resolve().is_relative_to(project):
                raise ValueError("プロジェクト外へのシーンリンクは登録できません: " + str(path))
            yield from path.iterdir()
        else:
            yield path


def latest_scenes(project, progress=None):
    project = Path(project).resolve()
    # Assets and their history are dependencies, not render jobs, when a scenes folder exists.
    base = project / "scenes"
    if not base.is_dir():
        base = project
    groups = {}
    count = 0
    if progress:
        progress({"phase": "シーンの更新日時を確認中（ハッシュ計算なし）", "path": str(base), "completed": 0, "total": None})
    for path in _scene_files(base, project):
        if path.suffix.lower() not in {".ma", ".mb"} or not path.is_file():
            continue
        relative = path.relative_to(project)
        if any(p.startswith(".") for p in relative.parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise ValueError("プロジェクト外へのシーンリンクは登録できません: " + str(path))
        stat = path.stat()
        key = scene_key(relative)
        record = {"logical": key, "relative": relative.as_posix(), "stat": [stat.st_size, stat.st_mtime_ns],
                  "mtime": stat.st_mtime, "stable": time.time() - stat.st_mtime >= 2}
        groups.setdefault(key, []).append(record)
        count += 1
        if progress:
            progress({"phase": "シーンの更新日時を確認中（ハッシュ計算なし）", "path": str(path), "completed": count, "total": None})
    result = {}
    for key, candidates in groups.items():
        # On exact timestamp ties, use a deterministic path.
        selected = max(candidates, key=lambda c: (c["stat"][1], c["relative"].casefold()))
        result[key] = {**selected, "candidates": len(candidates)}
    if progress:
        progress({"phase": "最新版シーンの選択完了", "path": str(base), "completed": count, "total": count})
    return result

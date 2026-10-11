from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .common import (FileLock, atomic_json, command_flags, digest, inside, installations,
                     read_json, runtime_env, sha256, worker_dir, maya_env, python_command)
from .store import Store, ACTIVE
from .catalog import latest_scenes, scene_key


def inventory(root, previous=None, progress=None):
    root = Path(root)
    previous = previous or {}
    result = {}
    paths = []
    def report(**event):
        if progress:
            progress(event)
    report(phase="ファイル一覧を取得中", path=str(root), completed=0, total=None)
    for path in root.rglob("*"):
        if not path.is_file() or any(p.startswith(".") for p in path.relative_to(root).parts):
            continue
        paths.append(path)
        report(phase="ファイル一覧を取得中", path=str(path), completed=len(paths), total=None)
    for index, path in enumerate(sorted(paths)):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("プロジェクト外へのリンクは素材フォルダとして追加してください: " + str(path))
        stat = path.stat()
        key = path.relative_to(root).as_posix()
        old = previous.get(key, {})
        signature = [stat.st_size, stat.st_mtime_ns]
        event = dict(phase="ファイルの変更を確認中", path=str(path), completed=index, total=len(paths),
                     file_bytes=0, file_size=stat.st_size)
        report(**event)
        def reading(count):
            report(**{**event, "file_bytes": count})
        value = old.get("hash") if old.get("stat") == signature else sha256(path, reading if progress else None)
        after = path.stat()
        if [after.st_size, after.st_mtime_ns] != signature or time.time() - stat.st_mtime < 2:
            raise RuntimeError("保存中のファイルを検出しました。次の更新で再確認します: " + str(path))
        result[key] = {"stat": signature, "hash": value}
    report(phase="ファイルの変更確認完了", path=str(root), completed=len(paths), total=len(paths))
    return result


def expand_asset(value, project):
    value = os.path.expandvars(value).replace("\\", "/")
    path = Path(value)
    if not path.is_absolute():
        path = Path(project) / path
    if path.is_file():
        return [path.resolve()]
    if path.is_dir():
        return sorted(p.resolve() for p in path.rglob("*") if p.is_file())
    pattern = re.sub(r"<UDIM>|<uvtile>|<f\d*>|#+|%0?\d*d|\$F\d*", "*", path.name, flags=re.I)
    if pattern != path.name and path.parent.is_dir():
        return sorted(p.resolve() for p in path.parent.glob(pattern) if p.is_file())
    return []


def portable_path(value, project, root):
    """Translate metadata recorded at export time after a USB drive/folder move."""
    origin = project.get("portable", {}).get("creation_root")
    if origin:
        old, prefix = value.replace("\\", "/"), origin.replace("\\", "/").rstrip("/")
        if old.casefold() == prefix.casefold() or old.casefold().startswith(prefix.casefold() + "/"):
            return str(Path(root) / old[len(prefix):].lstrip("/"))
    return value


def portable_identity(value, project, root):
    """Content identity must not depend on a USB drive letter or its mount path."""
    portable = project.get("portable")
    if not portable:
        return value
    if isinstance(value, dict):
        return {portable_identity(k, project, root): portable_identity(v, project, root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable_identity(v, project, root) for v in value]
    if isinstance(value, str):
        normal = value.replace("\\", "/")
        mappings = []
        for original, relative in portable["mappings"]:
            for base in (str(root), portable.get("creation_root", str(root))):
                mappings.append((str(Path(base) / relative).replace("\\", "/").rstrip("/"), original.replace("\\", "/").rstrip("/")))
        for source, target in sorted(mappings, key=lambda p: len(p[0]), reverse=True):
            if normal.casefold() == source.casefold() or normal.casefold().startswith(source.casefold() + "/"):
                return target + normal[len(source):]
    return value


def probe(root, project, scene, version, config=None, mode="inspect", payload=None):
    config = config or {}
    available = installations(config.get("maya_paths"))
    if version not in available:
        raise RuntimeError("Maya " + version + " がありません")
    work = worker_dir(root) / "probes" / uuid.uuid4().hex[:16]
    work.mkdir(parents=True)
    request = {"mode": mode, "project": str(project), "scene": str(scene), "result": str(work / "result.json"), **(payload or {})}
    atomic_json(work / "request.json", request)
    command = python_command(Path(available[version]) / "bin/mayapy.exe",
               Path(__file__).resolve().parent / "maya_entry.py", [work / "request.json"])
    with (work / "probe.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, env=maya_env(root, work, available[version]), cwd=work,
                                stdout=log, stderr=subprocess.STDOUT, **command_flags())
        try:
            proc.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            from .process import terminate_owned
            terminate_owned(proc)
            raise RuntimeError("シーン調査が60分で応答しませんでした: " + str(work / "probe.log"))
    result = read_json(work / "result.json")
    if proc.returncode or not result or result.get("error"):
        raise RuntimeError((result or {}).get("error", "Maya調査失敗") + "\nログ: " + str(work / "probe.log"))
    return result


def refresh_project(store, pid, config=None, inspect_fn=probe, progress=None, detailed_scenes=()):
    with FileLock(store.root / "data" / ("scan_" + pid + ".lock"), timeout=0):
        state = store.read()
        project = state["projects"][pid]
        def report(event):
            if progress:
                progress({"project": project["name"], **event})
        try:
            catalog = latest_scenes(store.project_path(project), report if progress else None)
            updated = {}
            for logical, candidate in catalog.items():
                sid = digest(pid + "/" + logical)[:24]
                old = state["scenes"].get(sid, {})
                source = [candidate["relative"], candidate["stat"]]
                if not candidate["stable"]:
                    # Do not fall back to an older History file while the newest is being saved.
                    updated[sid] = {**old, "id": sid, "project_id": pid, "name": logical, "relative": candidate["relative"],
                        "logical": logical, "source_stamp": source, "source_mtime": candidate["mtime"],
                        "candidate_count": candidate["candidates"], "updated": time.time(),
                        "maya_version": project["maya_version"], "state": "error", "metadata": {"inspection": "lightweight"},
                        "dependency_hashes": {}, "revision": digest(source), "saving": True,
                        "error": "最新版シーンを保存中です。次の更新で再確認します"}
                    continue
                if old.get("source_stamp") == source and not old.get("superseded") and not old.get("saving"):
                    if old.get("candidate_count") != candidate["candidates"]:
                        updated[sid] = {**old, "candidate_count": candidate["candidates"]}
                    continue
                updated[sid] = {"id": sid, "project_id": pid, "name": logical, "relative": candidate["relative"],
                    "logical": logical, "source_stamp": source, "source_mtime": candidate["mtime"],
                    "candidate_count": candidate["candidates"], "maya_version": project["maya_version"],
                    "updated": time.time(), "state": "updated" if old else "unrendered", "revision": digest(source),
                    "metadata": {"inspection": "lightweight"}, "dependency_hashes": {}, "error": None,
                    "latest": old.get("latest")}
            ids = {digest(pid + "/" + logical)[:24] for logical in catalog}
            with store.transaction() as s:
                s["projects"][pid].update(scene_inventory=catalog, scan_mode="scene_mtime", scanned=time.time(), error=None)
                for sid, old in s["scenes"].items():
                    if old["project_id"] == pid and sid not in ids:
                        old.update(state="missing", error="最新版の一覧から外れました",
                                   superseded=not old.get("source_stamp") or scene_key(old["relative"]) in catalog)
                for sid, record in updated.items():
                    current = s["scenes"].get(sid, {})
                    if current.get("source_stamp") == record.get("source_stamp") and record["state"] != "error" and not current.get("saving"):
                        current["candidate_count"] = record.get("candidate_count", current.get("candidate_count"))
                        continue
                    s["scenes"][sid] = record
                _hold_outdated_jobs(s)
            count = len(updated)
        except Exception as exc:
            with store.transaction() as s:
                s["projects"][pid].update(error=str(exc), scanned=time.time())
            raise
    if detailed_scenes:
        count += _refresh_detailed_project(store, pid, config, inspect_fn, progress, detailed_scenes)
    return count


def refresh_scene_list(store, pid, config=None, inspect_fn=probe, progress=None, force=False):
    """Manually refresh the date catalog and cache scene settings without asset hashing."""
    count = refresh_project(store, pid, config, inspect_fn, progress)
    with FileLock(store.root / "data" / ("scan_" + pid + ".lock"), timeout=0):
        state = store.read()
        project = state["projects"][pid]
        path = store.project_path(project)
        scenes = [sc for sc in state["scenes"].values() if sc["project_id"] == pid
                  and not sc.get("superseded") and sc.get("state") != "missing" and not sc.get("saving")]
        for index, scene in enumerate(scenes):
            info = scene.get("metadata", {})
            cached = (not force and scene.get("metadata_source_stamp") == scene.get("source_stamp")
                      and info.get("inspection") in {"catalog", "full"}
                      and all(key in info for key in ("ranges", "time_unit", "resolution", "non_default_cameras")))
            if progress:
                progress({"project": project["name"], "phase": "保存済みシーン情報を確認中" if cached else "Mayaでシーン情報を調査中（1シーン最大60分）",
                          "scene": scene["relative"], "path": str(path / scene["relative"]),
                          "completed": index, "total": len(scenes)})
            if cached:
                continue
            try:
                portable = project.get("portable")
                payload = {"lightweight": True, "catalog_only": True}
                if portable:
                    payload.update(sources=str(inside(store.root, portable["sources"])),
                        mappings=[[a, str(inside(store.root, b))] for a, b in portable["mappings"]],
                        metadata=portable.get("scene_metadata", {}).get(scene["relative"], info))
                metadata = inspect_fn(store.root, path, path / scene["relative"], project["maya_version"], config, payload=payload)
                latest = latest_scenes(path).get(scene.get("logical", scene_key(scene["relative"])))
                if not latest or [latest["relative"], latest["stat"]] != scene.get("source_stamp"):
                    raise RuntimeError("調査中に最新版シーンが変更されました。更新を確認してください")
                error = None
            except Exception as exc:
                metadata, error = {}, str(exc)
            with store.transaction() as current:
                record = current["scenes"].get(scene["id"])
                if not record or record.get("source_stamp") != scene.get("source_stamp"):
                    raise RuntimeError("別の操作でシーン一覧が変更されました。更新を確認してください")
                record.update(metadata=metadata, metadata_source_stamp=scene.get("source_stamp") if not error else None,
                              metadata_scanned=time.time(), error=error)
                if error:
                    record["state"] = "error"
                elif record["state"] == "error":
                    record["state"] = "updated"
                _hold_outdated_jobs(current)
            count += 1
        if progress:
            progress({"project": project["name"], "phase": "シーン一覧・情報を保存しました", "completed": len(scenes), "total": len(scenes)})
    return count


def _hold_outdated_jobs(state):
    for job in state["jobs"].values():
        scene = state["scenes"].get(job["scene_id"], {})
        if job["state"] in {"pending", "paused"} and (scene.get("revision") != job["scene_revision"] or scene.get("state") in {"missing", "error"}):
            job.update(state="held", error="元シーンが更新・削除されました")
            job["revision"] += 1


def _refresh_detailed_project(store, pid, config=None, inspect_fn=probe, progress=None, detailed_scenes=()):
    with FileLock(store.root / "data" / ("scan_" + pid + ".lock"), timeout=0):
        state = store.read()
        project = state["projects"][pid]
        path = store.project_path(project)
        def report(**event):
            if progress:
                progress({"project": project["name"], **event})
        def inventory_progress(event):
            report(**event)
        def scene_hash(source):
            key = str(source.resolve()).casefold()
            if key in hash_cache:
                return hash_cache[key]
            if not progress:
                value = sha256(source)
                hash_cache[key] = value
                return value
            report(phase="依存素材の変更を確認中", path=str(source), completed=scene_index,
                   total=len(scenes), scene=relative, file_bytes=0, file_size=source.stat().st_size)
            size = source.stat().st_size
            value = sha256(source, lambda count: report(phase="依存素材の変更を確認中", path=str(source),
                completed=scene_index, total=len(scenes), scene=relative, file_bytes=count, file_size=size))
            hash_cache[key] = value
            return value
        try:
            files = inventory(path, project.get("inventory"), inventory_progress if progress else None)
            hash_cache = {str((path / k).resolve()).casefold(): v["hash"] for k, v in files.items()}
            extras_inventory = {}
            for extra in project.get("extras", []):
                folder = Path(extra)
                if not folder.is_absolute():
                    folder = inside(store.root, folder)
                if folder.resolve() == store.root or store.root.is_relative_to(folder.resolve()):
                    raise ValueError("ファーム全体を追加素材に含めることはできません")
                extras_inventory[extra] = inventory(folder, project.get("extras_inventory", {}).get(extra), inventory_progress if progress else None)
            extras_hashes = {extra: {k: v["hash"] for k, v in values.items()} for extra, values in extras_inventory.items()}
            old_extras_hashes = {extra: {k: v["hash"] for k, v in values.items()} for extra, values in project.get("extras_inventory", {}).items()}
            extras_changed = extras_hashes != old_extras_hashes
            detail_by_relative = {state["scenes"][sid]["relative"]: sid for sid in detailed_scenes
                                  if sid in state["scenes"] and state["scenes"][sid]["project_id"] == pid}
            scenes = list(detail_by_relative)
            changed = {k for k in files.keys() | project.get("inventory", {}).keys()
                       if files.get(k, {}).get("hash") != project.get("inventory", {}).get(k, {}).get("hash")}
            expanded = {}
            def asset_paths(dep):
                if dep not in expanded:
                    expanded[dep] = expand_asset(resolve_project_asset(portable_path(dep, project, store.root), path), path)
                return expanded[dep]
            all_known = {str((path / sc["relative"]).resolve()).casefold()
                         for sc in state["scenes"].values() if sc["project_id"] == pid}
            for sc in state["scenes"].values():
                if sc["project_id"] == pid:
                    for dep in sc.get("tracked_dependencies", sc.get("metadata", {}).get("dependencies", [])):
                        all_known.update(str(p).casefold() for p in asset_paths(dep))
            changed_abs = {str((path / k).resolve()).casefold() for k in changed}
            unknown_change = bool(changed_abs - all_known) or extras_changed
            updated = {}
            for scene_index, relative in enumerate(scenes):
                report(phase="シーンの更新要否を確認中", path=str(path / relative), completed=scene_index, total=len(scenes), scene=relative)
                sid = detail_by_relative[relative]
                old = state["scenes"].get(sid, {})
                deps = old.get("tracked_dependencies", old.get("metadata", {}).get("dependencies", []))
                dep_hashes = {}
                for dep in deps:
                    matches = asset_paths(dep)
                    dep_hashes[dep] = [(str(p), scene_hash(p)) for p in matches]
                # Unknown project changes conservatively invalidate all scenes.
                known = {str((path / relative).resolve()).casefold()}
                known.update(str(p).casefold() for dep in deps for p in asset_paths(dep))
                needs = (not old or old.get("state") in {"missing", "error"} or bool(changed_abs & known)
                         or sid in detailed_scenes
                         or unknown_change or digest(portable_identity(dep_hashes, project, store.root)) != digest(portable_identity(old.get("dependency_hashes"), project, store.root)))
                if not needs:
                    continue
                record = {**old, "id": sid, "project_id": pid, "name": old.get("name", relative), "relative": relative,
                          "maya_version": project["maya_version"], "updated": time.time(), "state": "unrendered"}
                try:
                    candidate = latest_scenes(path).get(old.get("logical", scene_key(relative)))
                    if not candidate or [candidate["relative"], candidate["stat"]] != old.get("source_stamp"):
                        raise RuntimeError("最新版シーンが変更されました。更新を確認してください")
                    portable = project.get("portable")
                    extra_args = {"payload": {"sources": str(inside(store.root, portable["sources"])),
                        "mappings": [[a, str(inside(store.root, b))] for a, b in portable["mappings"]],
                        "metadata": portable.get("scene_metadata", {}).get(relative, old.get("metadata", {}))}} if portable else {"payload": {
                            "lightweight": sid not in detailed_scenes,
                            "mappings": project_path_mappings(path, path / relative) if sid in detailed_scenes else []}}
                    phase = "Mayaでシーンを詳細調査中" if portable or sid in detailed_scenes else "Mayaでシーンを軽量調査中（参照なし）"
                    report(phase=phase + "（1シーン最大60分）", path=str(path / relative),
                           completed=scene_index, total=len(scenes), scene=relative)
                    metadata = inspect_fn(store.root, path, path / relative, project["maya_version"], config, **extra_args)
                    report(phase="素材の読み替え・警告を確認中", scene=old.get("name", relative),
                           warnings=[{"scene": old.get("name", relative), "message": message}
                                     for message in metadata.get("warnings", [])],
                           mappings=[{"scene": old.get("name", relative), "source": source,
                                      "resolved": target, "target": ""}
                                     for source, target in metadata.get("path_mappings", [])])
                    missing = metadata.get("missing", [])
                    if missing:
                        raise ValueError("不足素材: " + "\n".join(missing))
                    tracked = set(metadata.get("dependencies", []))
                    if metadata.get("inspection") == "lightweight":
                        tracked.update(deps)  # Retain previously discovered external dependencies until the next full inspection.
                    dep_hashes = {dep: [(str(p), scene_hash(p)) for p in asset_paths(dep)] for dep in sorted(tracked)}
                    if sha256(path / relative) != files[relative]["hash"]:
                        raise RuntimeError("調査中にシーンが変更されました")
                    latest = latest_scenes(path).get(old.get("logical", scene_key(relative)))
                    if not latest or [latest["relative"], latest["stat"]] != old.get("source_stamp"):
                        raise RuntimeError("調査中に最新版シーンが変更されました")
                    record.update(metadata=metadata, dependency_hashes=dep_hashes, tracked_dependencies=sorted(tracked),
                        revision=digest(portable_identity([old.get("source_stamp"), files[relative]["hash"], dep_hashes, metadata,
                                         sorted((k, v["hash"]) for k, v in files.items()) if unknown_change else old.get("project_context"), extras_hashes], project, store.root)),
                        project_context=sorted((k, v["hash"]) for k, v in files.items()) if unknown_change else old.get("project_context"),
                        error=None)
                    if old and old.get("revision") == record["revision"]:
                        record["state"] = old["state"]
                    elif old:
                        record["state"] = "updated"
                    record["latest"] = old.get("latest")
                except Exception as exc:
                    record.update(state="error", error=str(exc), revision=old.get("revision", uuid.uuid4().hex), metadata={}, dependency_hashes={})
                updated[sid] = record
            report(phase="調査結果を保存中", path=str(path), completed=len(scenes), total=len(scenes))
            with store.transaction() as s:
                p = s["projects"][pid]
                p.update(inventory=files, extras_inventory=extras_inventory, scanned=time.time(), error=None)
                for sid, record in updated.items():
                    # Preserve a concurrent completion of the same revision.
                    current = s["scenes"].get(sid, {})
                    if current.get("revision") == record.get("revision") and record["state"] not in {"error", "missing"}:
                        record["state"] = current["state"]
                        record["latest"] = current.get("latest")
                    s["scenes"][sid] = record
                _hold_outdated_jobs(s)
            report(phase="プロジェクト調査完了", path=str(path), completed=len(scenes), total=len(scenes))
            return len(updated)
        except Exception as exc:
            with store.transaction() as s:
                s["projects"][pid].update(error=str(exc), scanned=time.time())
            raise


def copy_with_progress(source, destination, progress=None):
    if not progress:
        return shutil.copyfile(source, destination)
    size = Path(source).stat().st_size
    done = 0
    progress({"path": str(source), "file_bytes": done, "file_size": size})
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
            done += len(chunk)
            progress({"path": str(source), "file_bytes": done, "file_size": size})


def put_blob(root, source, expected, progress=None):
    destination = inside(root, "data/blobs/" + expected[:2] + "/" + expected)
    if destination.exists():
        if sha256(destination) != expected:
            raise ValueError("素材キャッシュが破損しています: " + str(destination))
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + "." + uuid.uuid4().hex[:16] + ".tmp")
    try:
        copy_with_progress(source, tmp, progress)
        if sha256(tmp) != expected or sha256(source) != expected:
            raise RuntimeError("コピー中に素材が変更されました: " + str(source))
        # Same content writers are safe; no hard links into mutable workspaces.
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def create_snapshot(store, scene, trust_saved=False, progress=None):
    def report(**event):
        if progress:
            progress(event)
    if scene.get("metadata", {}).get("inspection") == "lightweight":
        raise ValueError("軽量調査のみのシーンです。参照を含む詳細調査を先に実行してください")
    state = store.read()
    project = state["projects"][scene["project_id"]]
    path = store.project_path(project)
    if trust_saved and scene.get("source_stamp"):
        stat = inside(path, scene["relative"]).stat()
        if scene["source_stamp"] != [scene["relative"], [stat.st_size, stat.st_mtime_ns]]:
            raise ValueError("詳細調査後に選択シーンが変更されました。梱包をやり直してください")
    if not trust_saved:
        candidate = latest_scenes(path).get(scene.get("logical", scene_key(scene["relative"])))
        if not candidate or [candidate["relative"], candidate["stat"]] != scene.get("source_stamp"):
            raise ValueError("最新版シーンが変更されました。先に更新を確認してください")
    current = inventory(path, progress=progress)
    recorded = project.get("inventory", {})
    if not trust_saved and {k: v["hash"] for k, v in current.items()} != {k: v["hash"] for k, v in recorded.items()}:
        raise ValueError("詳細調査後に素材が更新されました。選択シーンの投入操作から詳細調査をやり直してください")
    sources = {str((path / k).resolve()): ("project/" + k, v["hash"]) for k, v in current.items()}
    mappings = [[str(path), "project"]]
    for original, local in scene.get("metadata", {}).get("path_mappings", []):
        local_path = Path(local).resolve()
        if local_path.is_relative_to(path.resolve()):
            suffix = local_path.relative_to(path.resolve()).as_posix()
            target = "project" if suffix == "." else "project/" + suffix
            mappings.append([original, target])
    dependencies = scene.get("dependency_hashes", {})
    if trust_saved:
        dependencies = {dep: [] for dep in scene.get("metadata", {}).get("dependencies", [])}
    for dep, records in dependencies.items():
        actual = [(str(p), sha256(p)) for p in expand_asset(resolve_project_asset(
            portable_path(dep, project, store.root), path, scene.get("metadata", {}).get("path_mappings", [])), path)]
        if trust_saved and not actual:
            raise ValueError("不足素材: " + dep)
        if not trust_saved and digest(portable_identity(actual, project, store.root)) != digest(portable_identity(records, project, store.root)):
            raise ValueError("外部素材が更新されました。投入前の詳細調査をやり直してください: " + dep)
        for source, hash_value in actual:
            p = Path(source)
            if source in sources:
                continue
            parent = p.parent
            target_dir = "external/" + digest(str(parent).casefold())[:20]
            mappings.append([str(parent), target_dir])
            sources[source] = (target_dir + "/" + p.name, hash_value)
            if trust_saved and p.suffix.lower() in {".ma", ".mb"}:
                # References may use sibling textures/caches that were not loaded by the catalog.
                for key, record in inventory(parent, progress=progress).items():
                    sources.setdefault(str((parent / key).resolve()), (target_dir + "/" + key, record["hash"]))
    for extra in project.get("extras", []):
        folder = Path(extra)
        if not folder.is_absolute():
            folder = inside(store.root, folder)
        if not folder.is_dir():
            raise ValueError("追加素材フォルダがありません: " + extra)
        if folder.resolve() == store.root or store.root.is_relative_to(folder.resolve()):
            raise ValueError("ファーム全体を追加素材に含めることはできません")
        extra_files = inventory(folder, progress=progress)
        recorded_extra = project.get("extras_inventory", {}).get(extra, {})
        if not trust_saved and {k: v["hash"] for k, v in extra_files.items()} != {k: v["hash"] for k, v in recorded_extra.items()}:
            raise ValueError("追加素材が更新されました。投入前の詳細調査をやり直してください")
        target_dir = "external/" + digest(str(folder).casefold())[:20]
        mappings.append([str(folder), target_dir])
        for key, record in extra_files.items():
            source = str((folder / key).resolve())
            sources.setdefault(source, (target_dir + "/" + key, record["hash"]))
    if project.get("portable"):
        # Include the original-to-packaged mapping as well as this PC's current paths.
        for original, relative in project["portable"]["mappings"]:
            current_dir = str(inside(store.root, relative))
            for local, target in list(mappings):
                if current_dir.casefold() == local.casefold():
                    mappings.append([original, target])
                    break
    files, file_stats = {}, {}
    for index, (source, (relative, hash_value)) in enumerate(sources.items()):
        stat = Path(source).stat()
        report(phase="素材キャッシュを作成・検証中", path=source, completed=index, total=len(sources))
        put_blob(store.root, source, hash_value,
                 (lambda event: report(phase="素材キャッシュをコピー中", completed=index, total=len(sources), **event)) if progress else None)
        files[relative] = hash_value
        file_stats[relative] = [stat.st_size, stat.st_mtime_ns]
    # Recheck after copying the entire set, not just each file independently.
    for index, (source, (relative, value)) in enumerate(sources.items()):
        report(phase="梱包元の変更を再確認中", path=source, completed=index, total=len(sources))
        reading = (lambda count: report(phase="梱包元の変更を再確認中", path=source, completed=index,
                                       total=len(sources), file_bytes=count, file_size=Path(source).stat().st_size)) if progress else None
        if sha256(source, reading) != value:
            raise RuntimeError("梱包中に素材が更新されました: " + source)
        stat = Path(source).stat()
        if [stat.st_size, stat.st_mtime_ns] != file_stats[relative]:
            raise RuntimeError("梱包中に素材の更新日時が変わりました: " + source)
    snapshot = {"schema": 1, "scene": "project/" + scene["relative"], "project": "project",
                "files": files, "file_stats": file_stats, "mappings": sorted(set(tuple(x) for x in mappings)),
                "metadata": stored_job_metadata(scene), "source_revision": scene["revision"]}
    identity = digest(snapshot)
    relative = "data/snapshots/" + identity + ".json"
    atomic_json(inside(store.root, relative), snapshot)
    return relative


def materialize(root, snapshot, destination, progress=None):
    destination = inside(root, Path(destination).resolve().relative_to(Path(root).resolve()))
    for index, (relative, value) in enumerate(snapshot["files"].items()):
        if progress:
            progress({"phase": "プロジェクトを展開・検証中", "path": relative,
                      "completed": index, "total": len(snapshot["files"])})
        target = inside(destination, relative)
        stamp = snapshot.get("file_stats", {}).get(relative)
        if target.exists() and sha256(target) == value:
            if stamp:
                os.utime(target, ns=(stamp[1], stamp[1]))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = inside(root, "data/blobs/" + value[:2] + "/" + value)
        if sha256(blob) != value:
            raise ValueError("素材キャッシュのハッシュが一致しません")
        tmp = target.with_name(target.name + "." + uuid.uuid4().hex[:16] + ".tmp")
        copy_with_progress(blob, tmp, (lambda event: progress({"phase": "プロジェクトを展開中",
            "completed": index, "total": len(snapshot["files"]), **event})) if progress else None)
        if sha256(tmp) != value:
            raise ValueError("素材コピーの検証に失敗しました")
        os.replace(tmp, target)
        if stamp:
            os.utime(target, ns=(stamp[1], stamp[1]))
    if progress:
        progress({"phase": "プロジェクトの展開完了", "completed": len(snapshot["files"]), "total": len(snapshot["files"])})


def validate_settings(settings, metadata):
    result = copy.deepcopy(metadata["settings"])
    source = settings.get("range_source", result.get("range_source"))
    if source and source != "manual":
        if source not in metadata.get("ranges", {}):
            raise ValueError("範囲情報がありません。シーンを再調査してください")
        result.update(metadata["ranges"][source])
    result.update(settings)
    for field in ("start", "end", "step", "width", "height", "aa", "restart_limit"):
        if field in result:
            if float(result[field]) != int(result[field]):
                raise ValueError("現在のレンダーは整数フレームに対応しています。範囲・刻みを整数で指定してください")
            result[field] = int(result[field])
    if result["end"] < result["start"] or result["step"] < 1 or min(result["width"], result["height"]) < 1:
        raise ValueError("フレーム範囲または解像度が不正です")
    if result["format"] not in {"png", "exr"} or result["device"] not in {"CPU", "GPU"}:
        raise ValueError("PNG/EXRとCPU/GPUを指定してください")
    for field in ("cameras", "layers"):
        allowed = {x["name"] for x in metadata[field]}
        if not result[field] or not set(result[field]) <= allowed:
            raise ValueError(field + " の選択が不正です")
    if not set(result.get("aovs", [])) <= {x["name"] for x in metadata.get("aovs", [])}:
        raise ValueError("AOVの選択が不正です")
    result["timeout_minutes"] = float(result.get("timeout_minutes", 30))
    result["restart_limit"] = int(result.get("restart_limit", 3))
    if result["timeout_minutes"] <= 0 or result["restart_limit"] < 0:
        raise ValueError("タイムアウトは正数、再起動上限は0以上にしてください")
    from fractions import Fraction
    if Fraction(str(result["fps"])) <= 0:
        raise ValueError("FPSが不正です")
    post = result.setdefault("post", {"video": False, "fastdvdnet": False})
    if (post.get("video") or post.get("fastdvdnet")) and result["format"] != "png":
        raise ValueError("動画化・FastDVDnetにはPNG Beautyを指定してください。EXR/AOVは画像出力に対応します")
    return result


def ensure_detailed(store, scene_ids, config=None, inspect_fn=probe, progress=None, selected_only=False):
    """Resolve reference-dependent settings before a settings dialog, enqueue, or export."""
    state = store.read()
    groups = {}
    for sid in scene_ids:
        scene = state["scenes"][sid]
        groups.setdefault(scene["project_id"], []).append(sid)
    if config is None:
        config = read_json(worker_dir(store.root) / "config.json", {})
    if selected_only:
        for pid, ids in groups.items():
            with FileLock(store.root / "data" / ("scan_" + pid + ".lock"), timeout=0):
                project = state["projects"][pid]
                path = store.project_path(project)
                for index, sid in enumerate(ids):
                    scene = state["scenes"][sid]
                    source = inside(path, scene["relative"])
                    def report(**event):
                        if progress:
                            progress({"project": project["name"], "scene": scene["name"],
                                      "completed": index, "total": len(ids), **event})
                    before = source.stat()
                    if time.time() - before.st_mtime < 2:
                        raise ValueError("選択シーンを保存中です: " + str(source))
                    report(phase="選択シーンをMayaで詳細調査中（最大60分）", path=str(source))
                    portable = project.get("portable")
                    payload = {"lightweight": False, "mappings": project_path_mappings(path, source)}
                    if portable:
                        payload = {"sources": str(inside(store.root, portable["sources"])),
                                   "mappings": [[a, str(inside(store.root, b))] for a, b in portable["mappings"]],
                                   "metadata": portable.get("scene_metadata", {}).get(scene["relative"], scene.get("metadata", {}))}
                    def inspection_failure(exc):
                        with store.transaction() as current:
                            record = current["scenes"][sid]
                            if record.get("revision") == scene.get("revision"):
                                record.update(state="error", error=str(exc))
                                _hold_outdated_jobs(current)
                    try:
                        info = inspect_fn(store.root, path, source, project["maya_version"], config, payload=payload)
                    except Exception as exc:
                        inspection_failure(exc)
                        raise
                    report(phase="素材の読み替え・警告を確認中",
                           warnings=[{"scene": scene["name"], "message": message} for message in info.get("warnings", [])],
                           mappings=[{"scene": scene["name"], "source": old, "resolved": new, "target": ""}
                                     for old, new in info.get("path_mappings", [])])
                    if info.get("missing"):
                        error = ValueError("不足素材: " + "\n".join(info["missing"]))
                        inspection_failure(error)
                        raise error
                    if info.get("inspection") == "lightweight":
                        error = ValueError("選択シーンの詳細調査を完了できませんでした")
                        inspection_failure(error)
                        raise error
                    after = source.stat()
                    stamp = [after.st_size, after.st_mtime_ns]
                    if stamp != [before.st_size, before.st_mtime_ns]:
                        raise ValueError("詳細調査中に選択シーンが変更されました: " + str(source))
                    with store.transaction() as current:
                        record = current["scenes"][sid]
                        if record.get("revision") != scene.get("revision") or record["relative"] != scene["relative"]:
                            raise ValueError("調査中にシーン一覧が変更されました。梱包をやり直してください")
                        record.update(metadata=info, dependency_hashes={}, tracked_dependencies=info.get("dependencies", []),
                                      source_stamp=[scene["relative"], stamp], source_mtime=after.st_mtime,
                                      revision=digest([scene["relative"], stamp, info]), state="unrendered", error=None, saving=False)
                        _hold_outdated_jobs(current)
        return scene_ids
    for pid, ids in groups.items():
        refresh_project(store, pid, config, inspect_fn=inspect_fn, progress=progress, detailed_scenes=ids)
    state = store.read()
    for sid in scene_ids:
        scene = state["scenes"][sid]
        if scene["state"] in {"missing", "error"} or scene.get("metadata", {}).get("inspection") == "lightweight":
            raise ValueError(scene.get("error") or "詳細調査を完了できませんでした: " + scene["name"])
    return scene_ids


def resolve_project_asset(value, project, mappings=()):
    """Resolve by complete project-relative path, never by basename."""
    old = value.replace("\\", "/")
    for source, target in sorted(mappings, key=lambda m: len(m[0]), reverse=True):
        source = source.replace("\\", "/").rstrip("/")
        if old.casefold() == source.casefold() or old.casefold().startswith(source.casefold() + "/"):
            return target.replace("\\", "/").rstrip("/") + old[len(source):]
    project = Path(project).resolve()
    match = re.search(r"(?:^|/)" + re.escape(project.name) + r"/+(.*)$", old, re.IGNORECASE)
    if match:
        target = (project / match.group(1)).resolve()
        if target.is_relative_to(project) and expand_asset(str(target), project):
            return str(target)
    return value


def project_path_mappings(project, scene):
    """Find saved Windows project roots for runtime directory mapping, without editing files."""
    project, scene = Path(project), Path(scene)
    # Maya .ma/.mb both store reference paths as strings. Match complete root segments.
    pattern = re.compile(rb"(?:[A-Za-z]:|[\\/]{2}[^\\/\x00-\x1f\"\r\n]+)[\\/][^\x00-\x1f\"\r\n]{0,1024}?[\\/]" +
                         re.escape(project.name.encode("utf-8")) + rb"(?=[\\/])", re.IGNORECASE)
    roots = set()
    with scene.open("rb") as stream:
        tail = b""
        while chunk := stream.read(1024 * 1024):
            data = tail + chunk
            for match in pattern.finditer(data):
                raw_root = match.group().decode("utf-8", errors="strict").replace("\\", "/")
                root = ("/" if raw_root.startswith("//") else "") + re.sub(r"/+", "/", raw_root)
                if root.casefold() != str(project).replace("\\", "/").casefold():
                    roots.add(root)
            tail = data[-4096:]
    explicit = read_json(project / "asset_paths.json", {})
    pairs = explicit.get("mappings", [])
    if not isinstance(pairs, list):
        raise ValueError("asset_paths.json の mappings は配列にしてください")
    configured = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) and x for x in pair):
            raise ValueError("asset_paths.json: 読み替えは [旧パス, 新パス] で指定してください")
        target = Path(pair[1])
        configured.append([pair[0], str(target if target.is_absolute() else project / target)])
    overridden = {p[0].replace("\\", "/").rstrip("/").casefold() for p in configured}
    return configured + [[root, str(project)] for root in sorted(roots) if root.casefold() not in overridden]


def stored_job_metadata(scene):
    """Use stored scene data, with editable defaults for older basic catalogs."""
    info = copy.deepcopy(scene.get("metadata", {}))
    if not info or info.get("inspection") == "lightweight":
        raise ValueError("シーン情報がありません。「更新を確認」を実行してください")
    info.setdefault("layers", [{"name": "defaultRenderLayer", "enabled": True, "kind": "legacy"}])
    info.setdefault("aovs", [])
    for aov in info["aovs"]:
        aov.setdefault("layers", [l["name"] for l in info["layers"]])
    defaults = {"format": "png", "device": "CPU", "aa": 3, "adaptive": False, "oidn": "scene",
                "post": {"video": False, "fastdvdnet": False},
                "layers": [l["name"] for l in info["layers"] if l.get("enabled", True)],
                "aovs": [a["name"] for a in info["aovs"]]}
    info["settings"] = {**defaults, **info.get("settings", {})}
    return info


def enqueue(store, scene_id, overrides=None, prepared_revision=None, maya_version=None):
    state = store.read()
    scene = state["scenes"][scene_id]
    if prepared_revision is not None and scene["revision"] != prepared_revision:
        raise ValueError("シーンが更新されました。設定を確認し直してください")
    if scene["state"] in {"missing", "error"}:
        raise ValueError(scene.get("error") or "元ファイルがありません")
    settings = validate_settings({**{k: state["settings"][k] for k in ("timeout_minutes", "restart_limit")}, **(overrides or {})}, stored_job_metadata(scene))
    source = {"project_id": scene["project_id"], "relative": scene["relative"],
              "metadata": stored_job_metadata(scene)}
    return store.enqueue_record(scene_id, scene["revision"], None, settings, source=source,
                                maya_version=maya_version)

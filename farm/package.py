from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import time

from .assets import create_snapshot, materialize, refresh_project, enqueue, probe, ensure_detailed, copy_with_progress
from .common import atomic_json, inside, read_json, sha256
from .store import Store


def filesystem_type(path):
    if os.name != "nt":
        return "unknown"
    import ctypes
    volume = ctypes.create_unicode_buffer(1024)
    fs = ctypes.create_unicode_buffer(256)
    if not ctypes.windll.kernel32.GetVolumePathNameW(str(path), volume, len(volume)):
        return "unknown"
    if not ctypes.windll.kernel32.GetVolumeInformationW(volume.value, None, 0, None, None, None, fs, len(fs)):
        return "unknown"
    return fs.value


def export_package(store, destination, scene_ids, config=None, progress=None):
    warnings = []
    mapping_records = []
    def report(**event):
        if progress:
            progress(event)
    def detail_report(event):
        for warning in event.get("warnings", []):
            if warning not in warnings:
                warnings.append(warning)
        for mapping in event.get("mappings", []):
            if mapping not in mapping_records:
                mapping_records.append(mapping)
        if "warnings" in event or "mappings" in event:
            event = {**event, "warnings": list(warnings), "mappings": list(mapping_records)}
        report(**event)
    report(phase="梱包の準備中", total=None)
    destination = Path(destination).resolve()
    if destination == store.root or store.root.is_relative_to(destination):
        raise ValueError("既存ファーム自身またはその親には書き出せません")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("空の書き出し先フォルダを指定してください")
    if not scene_ids:
        raise ValueError("シーンを選択してください")
    ensure_detailed(store, scene_ids, config, progress=detail_report, selected_only=True)
    state = store.read()
    snapshots = {}
    total = 0
    largest = 0
    for index, sid in enumerate(scene_ids):
        scene = state["scenes"][sid]
        if scene["state"] in {"missing", "error"}:
            raise ValueError("不足素材やシーンエラーを解消してから梱包してください")
        for message in scene.get("metadata", {}).get("warnings", []):
            warning = {"scene": scene["name"], "message": message}
            if warning not in warnings:
                warnings.append(warning)
        report(phase="必要素材を収集中", warnings=list(warnings), mappings=list(mapping_records))
        report(phase="必要素材を収集中", scene=scene["name"], completed=index, total=len(scene_ids))
        rel = create_snapshot(store, scene, trust_saved=True, progress=progress)
        snapshots[sid] = read_json(inside(store.root, rel))
        local_mappings = dict(scene.get("metadata", {}).get("path_mappings", []))
        for source, relative in snapshots[sid]["mappings"]:
            mapping = {"scene": scene["name"], "source": source, "resolved": local_mappings.get(source, ""),
                       "target": (Path("projects") / scene["project_id"] / relative).as_posix()}
            mapping_records = [m for m in mapping_records if (m["scene"], m["source"]) != (mapping["scene"], source)]
            mapping_records.append(mapping)
        report(phase="素材の対応表を作成中", warnings=list(warnings), mappings=list(mapping_records))
    unique_hashes = {h for snapshot in snapshots.values() for h in snapshot["files"].values()}
    for value in unique_hashes:
        size = inside(store.root, "data/blobs/" + value[:2] + "/" + value).stat().st_size
        total += size
        largest = max(largest, size)
    destination.mkdir(parents=True, exist_ok=True)
    # Blobs plus unpacked projects; workers later need another source copy and output capacity.
    if shutil.disk_usage(destination).free < total * 3 + 32 * 1024 * 1024:
        raise ValueError("梱包・実行用素材の空き容量が不足しています（画像出力分は別途必要です）")
    if filesystem_type(destination).upper() in {"FAT", "FAT32"} and largest >= 4 * 1024 ** 3:
        raise ValueError("FAT32の単一ファイル上限を超えます。exFATまたはNTFSを使用してください")
    atomic_json(destination / "package_status.json", {"ready": False, "phase": "copying"})
    atomic_json(destination / "package_warnings.json", warnings)
    atomic_json(destination / "package_mappings.json", mapping_records)
    try:
        report(phase="ツールをコピー中", path=str(destination), total=None)
        code_root = Path(__file__).resolve().parent.parent
        for name in ("farm_cli.py", "launch.ps1", "起動.cmd", "start.cmd", "README.md", "VALIDATION.md"):
            source = code_root / name
            if source.exists():
                shutil.copy2(source, destination / name)
        shutil.copytree(code_root / "farm", destination / "farm", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target = Store(destination)
        copied_bytes = 0
        for index, value in enumerate(sorted(unique_hashes)):
            rel = "data/blobs/" + value[:2] + "/" + value
            dest = inside(destination, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_with_progress(inside(store.root, rel), dest,
                (lambda event: report(phase="保存先へ素材をコピー中", completed=index, total=len(unique_hashes),
                                      copied_bytes=copied_bytes + event["file_bytes"], total_bytes=total, **event)) if progress else None)
            report(phase="保存先の素材を検証中", path=str(dest), completed=index, total=len(unique_hashes))
            if sha256(dest) != value:
                raise ValueError("USBへの素材コピーに失敗しました")
            copied_bytes += dest.stat().st_size
            report(phase="保存先へ素材をコピー中", completed=index + 1, total=len(unique_hashes),
                   copied_bytes=copied_bytes, total_bytes=total)
        grouped = {}
        for sid, snapshot in snapshots.items():
            pid = state["scenes"][sid]["project_id"]
            combined = grouped.setdefault(pid, {"files": {}, "file_stats": {}, "mappings": []})
            for path, value in snapshot["files"].items():
                if path in combined["files"] and combined["files"][path] != value:
                    raise ValueError("同じプロジェクトの素材版が一致しません")
                combined["files"][path] = value
            combined["file_stats"].update(snapshot.get("file_stats", {}))
            combined["mappings"].extend(snapshot["mappings"])
        ids = {}
        for pid, snapshot in grouped.items():
            source_project = state["projects"][pid]
            base = destination / "projects" / pid
            report(phase="プロジェクトを展開中", project=source_project["name"], total=None)
            materialize(destination, snapshot, base, progress=progress)
            new_pid = target.add_project(base / "project", source_project["maya_version"], source_project["name"])
            ids[pid] = new_pid
            portable = {"sources": base.relative_to(destination).as_posix(), "creation_root": str(destination),
                        "scene_metadata": {state["scenes"][sid]["relative"]: snap["metadata"] for sid, snap in snapshots.items()
                                           if state["scenes"][sid]["project_id"] == pid},
                        "mappings": [[source, (base / relative).relative_to(destination).as_posix()]
                                     for source, relative in sorted(set(tuple(m) for m in snapshot["mappings"]))]}
            with target.transaction() as s:
                s["projects"][new_pid]["portable"] = portable
                s["projects"][new_pid]["extras"] = [(base / "external").relative_to(destination).as_posix()] if (base / "external").exists() else []
        report(phase="コピー完了後の安定待ち", total=None)
        time.sleep(2.1)  # Let the stable-file guard distinguish completed copies from active writes.
        jobs = []
        for pid, new_pid in ids.items():
            refresh_project(target, new_pid, config, detailed_scenes=(), progress=progress)
        refreshed = target.read()
        for index, sid in enumerate(scene_ids):
            report(phase="持ち出し用キューを作成中", completed=index, total=len(scene_ids))
            scene = state["scenes"][sid]
            new_scene = next(s for s in refreshed["scenes"].values() if s["project_id"] == ids[scene["project_id"]] and s["relative"] == scene["relative"])
            if new_scene["state"] == "error":
                raise ValueError("持ち出し先で素材検査に失敗: " + new_scene["error"])
            with target.transaction() as destination_state:
                destination_state["scenes"][new_scene["id"]]["metadata"] = state["scenes"][sid]["metadata"]
            jobs.append(enqueue(target, new_scene["id"]))
        # Environment paths are intentionally not copied from the source PC. Probe caches
        # may exist under workers, but no claim or running status is exported.
        package_report = {"ready": True, "farm_id": target.read()["farm_id"], "jobs": jobs,
                  "projects": len(ids), "source_bytes": total, "completed": time.time(), "warnings": warnings,
                  "mappings": mapping_records,
                  "note": "任意の後処理環境は使用するPCの設定画面で指定してください"}
        atomic_json(destination / "package_status.json", package_report)
        report(phase="梱包完了", completed=len(scene_ids), total=len(scene_ids))
        return package_report
    except Exception as exc:
        atomic_json(destination / "package_status.json", {"ready": False, "phase": "failed", "error": str(exc)})
        raise

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time

from .common import command_flags, inside, sha256, digest, read_json, atomic_json


def image_info(path, oiio):
    path = Path(path)
    if not oiio or not Path(oiio).is_file():
        raise RuntimeError("Arnold付属oiiotool.exeが必要です")
    result = subprocess.run([str(oiio), "--info", "-v", "--stats", path.name], cwd=path.parent,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=180, **command_flags())
    if result.returncode or "error" in result.stderr.lower():
        raise ValueError("画像を読み込めません: " + str(path) + "\n" + result.stderr)
    dimensions = re.search(r"(\d+)\s*x\s*(\d+),\s*(\d+)\s*channel", result.stdout)
    channels = re.search(r"channel list:\s*([^\r\n]+)", result.stdout)
    if not dimensions or not channels:
        raise ValueError("画像情報を解釈できません: " + result.stdout[:1000])
    return {"width": int(dimensions[1]), "height": int(dimensions[2]),
            "channels": [s.strip() for s in channels[1].split(",")], "bytes": path.stat().st_size,
            "sha256": sha256(path)}


def collect_outputs(directory, settings, aovs, oiio, frame=None):
    directory = Path(directory)
    files = sorted(p for p in directory.rglob("*") if p.suffix.lower() in {".png", ".exr"})
    if not files:
        raise ValueError("レンダリング画像が作成されませんでした")
    result, found = [], set()
    for path in files:
        relative = path.relative_to(directory).as_posix()
        parts = [p.casefold() for p in path.relative_to(directory).parts]
        stem = path.stem.casefold()
        def matches(name):
            return bool(re.fullmatch(re.escape(name.casefold()) + r"(?:[_.]-?\d+)?", stem)) or name.casefold() in parts
        pass_name = next((name for name in aovs if matches(name)), None)
        if pass_name is None:
            pass_name = next((name for name in aovs if name.startswith("crypto_") and re.fullmatch(re.escape(name.casefold()) + r"\d+(?:[_.]-?\d+)?", stem)), None)
        if pass_name is None and any(matches(name) for name in ("beauty", "rgba", "rgb")):
            pass_name = "beauty"
        if pass_name is None:
            raise ValueError("出力パスとAOVを対応できません: " + relative)
        info = image_info(path, oiio)
        if (info["width"], info["height"]) != (settings["width"], settings["height"]):
            raise ValueError("画像寸法が一致しません: " + relative)
        if pass_name == "beauty" and not {"R", "G", "B"} <= set(info["channels"]):
            raise ValueError("Beauty RGBチャンネルがありません")
        if pass_name in found and not pass_name.startswith("crypto_"):
            raise ValueError("同じAOVの複数出力があります: " + pass_name)
        found.add(pass_name)
        result.append({"path": relative, "pass": pass_name, **info})
    missing = {"beauty", *aovs} - found
    if missing:
        raise ValueError("不足した出力: " + ", ".join(sorted(missing)))
    if frame is not None:
        renames = []
        targets = set()
        for file in result:
            source = directory / file["path"]
            name = file["pass"]
            if name.startswith("crypto_"):
                # Retain Cryptomatte's numbered output groups, removing only a frame suffix.
                name = re.sub(r"[_.]-?\d+$", "", source.stem)
            target = source.with_name(f"{name}.{int(frame):06d}{source.suffix.lower()}")
            if target in targets or (target != source and target.exists()):
                raise ValueError("出力ファイル名が重複します: " + str(target))
            targets.add(target)
            renames.append((source, target, file))
        for source, target, file in renames:
            if source != target:
                source.rename(target)
            file["path"] = target.relative_to(directory).as_posix()
    return result


def output_prefix(layer, camera):
    def label(value):
        return re.sub(r'[^\w.-]+', '_', value).strip(' ._')[:32] or "unnamed"
    return label(layer) + "__" + label(camera.split("|")[-1]) + "__" + digest([layer, camera])[:8] + "__"


def migrate_raw_checkpoint(work):
    """Caller must hold the job run lock with Maya stopped. Copy, commit, then remove originals."""
    import copy
    import shutil
    work = Path(work).resolve()
    checkpoint = work / "frames.json"
    original = read_json(checkpoint, {})
    frames = copy.deepcopy(original)
    plan = []
    targets = set()
    for key, record in frames.items():
        verify_record(work, record)
        for file in record["files"]:
            source = inside(work, file["path"])
            if source.parent == work / "raw":
                continue
            if not source.is_relative_to(work / "raw"):
                raise ValueError("raw外の画像は移行できません")
            name = file["pass"]
            if name.startswith("crypto_"):
                name = re.sub(r"[_.]-?\d+$", "", source.stem)
            leaf = output_prefix(record["layer"], record["camera"]) + f"{name}.{int(record['frame']):06d}{source.suffix.lower()}"
            target = inside(work, "raw/" + leaf)
            if str(target).casefold() in targets:
                raise ValueError("移行先の画像名が重複します")
            targets.add(str(target).casefold())
            if target.exists() and sha256(target) != file["sha256"]:
                raise ValueError("移行先に異なる画像があります: " + str(target))
            plan.append({"source": file["path"], "target": target.relative_to(work).as_posix(), "sha256": file["sha256"]})
            file["path"] = target.relative_to(work).as_posix()
    if not plan:
        return {"moved": 0, "files": []}
    archive = work / "migration" / str(time.time_ns())
    archive.mkdir(parents=True)
    atomic_json(archive / "frames_before.json", original)
    atomic_json(archive / "plan.json", plan)
    for item in plan:
        source, target = inside(work, item["source"]), inside(work, item["target"])
        if not target.exists():
            shutil.copyfile(source, target)
        if sha256(target) != item["sha256"]:
            raise ValueError("移行先の画像が一致しません")
    for record in frames.values():
        verify_record(work, record)
    atomic_json(checkpoint, frames)
    # Originals are retained until the complete new checkpoint has been committed.
    for item in plan:
        source = inside(work, item["source"])
        if sha256(source) == item["sha256"]:
            source.unlink()
        parent = source.parent
        while parent != work / "raw" and parent.is_relative_to(work / "raw"):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    report = {"moved": len(plan), "files": plan, "checkpoint_backup": (archive / "frames_before.json").relative_to(work).as_posix()}
    atomic_json(work / "raw_migration_report.json", report)
    return report


def flatten_outputs(work, directory, files, layer, camera, unit):
    work = Path(work).resolve()
    directory = Path(directory).resolve()
    if not directory.is_relative_to(work):
        raise ValueError("作業領域外の画像は移動できません")
    prefix = output_prefix(layer, camera)
    names = [prefix + Path(file["path"]).name for file in files]
    if len(set(name.casefold() for name in names)) != len(names):
        raise ValueError("raw直下の画像名が重複します")
    result = []
    for file in files:
        source = inside(directory, file["path"])
        target = inside(work, "raw/" + prefix + source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            if target.exists():
                quarantine = inside(work, "quarantine/" + unit + "_" + str(time.time_ns()) + "/" + target.name)
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                target.rename(quarantine)
            source.rename(target)
        result.append({**file, "path": target.relative_to(work).as_posix()})
    return result


def verify_record(work, record, oiio=None):
    for file in record["files"]:
        path = inside(work, file["path"])
        if sha256(path) != file["sha256"]:
            raise ValueError("途中画像が破損しています: " + str(path))
        if oiio:
            info = image_info(path, oiio)
            if any(info[k] != file[k] for k in ("width", "height", "channels")):
                raise ValueError("画像情報が記録と一致しません")

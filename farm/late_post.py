"""Run optional processing from immutable published Beauty frames, without rerendering."""
from pathlib import Path
from collections import defaultdict
from fractions import Fraction
import subprocess
import sys
import time
import uuid
import shutil

from .common import FileLock, atomic_json, read_json, inside, sha256, digest, worker_dir, runtime_env, command_flags, python_command
from .post import check_dependencies
from .process import ProcessGroup
from .results import vacant


def published_groups(store, job_id):
    job = store.read()["jobs"][job_id]
    if not job.get("publication"):
        raise ValueError("レンダリングが完了して公開されたジョブを選択してください")
    source = inside(store.root, job["publication"])
    manifest = read_json(source / "manifest.json", {})
    if not manifest.get("verified") or manifest.get("job", {}).get("id") != job_id:
        raise ValueError("成果物の検証記録がないか、ジョブが一致しません")
    groups = defaultdict(list)
    for frame in manifest["frames"].values():
        beauty = [f for f in frame["files"] if f.get("pass") == "beauty"]
        if len(beauty) != 1:
            raise ValueError("Beauty画像を一つに特定できません")
        groups[(frame["layer"], frame["camera"])].append((frame["frame"], beauty[0]))
    return job, source, manifest, groups


def run_late_postprocess(store, job_id, options, config, selection=None, fps=None, progress=None, cancelled=None):
    options = {"video": bool(options.get("video")), "fastdvdnet": bool(options.get("fastdvdnet"))}
    if not any(options.values()):
        raise ValueError("動画化かFastDVDnetを選択してください")
    job, source, manifest, groups = published_groups(store, job_id)
    chosen = list(groups) if selection is None else [tuple(pair) for pair in selection]
    if not chosen or len(set(chosen)) != len(chosen) or any(pair not in groups for pair in chosen):
        raise ValueError("対象のレイヤー・カメラを選択してください")
    fps = str(fps or manifest["job"]["settings"]["fps"])
    if Fraction(fps) <= 0:
        raise ValueError("FPSは正の値にしてください")
    for pair in chosen:
        if any(Path(file["path"]).suffix.lower() != ".png" for _, file in groups[pair]):
            raise ValueError("追加後処理はPNGのBeauty連番に対応しています。EXRは未対応です")
    def report(**event):
        if progress:
            progress(event)
    def guard():
        if cancelled and cancelled():
            raise InterruptedError("追加後処理を停止しました。原本画像は変更していません")
    with FileLock(store.root / "data" / ("late_post_" + job_id + ".lock"), timeout=0):
        # A legacy publication may have moved while the dialog was open.
        job, source, manifest, groups = published_groups(store, job_id)
        report(phase="後処理環境を確認中")
        guard()
        caps = check_dependencies(store.root, config, required={key for key, enabled in
                                  [('ffmpeg', options['video']), ('fastdvdnet', options['fastdvdnet'])] if enabled})
        if any(options[k] and not caps[k if k == "fastdvdnet" else "ffmpeg"] for k in options):
            raise ValueError("必要なFFmpeg・FastDVDnet環境がありません。設定と環境診断を確認してください")
        run_id = uuid.uuid4().hex[:16]
        while (worker_dir(store.root) / "postprocess" / job_id / run_id).exists():
            run_id = uuid.uuid4().hex[:16]
        work = worker_dir(store.root) / "postprocess" / job_id / run_id
        work.mkdir(parents=True)
        result_rel = vacant(store.root, job["publication"] + "/postprocess/" + time.strftime('%Y%m%d_%H%M%S'))
        record = {"id": run_id, "state": "running", "created": time.time(), "options": options, "fps": fps,
                  "source": job["publication"], "work": work.relative_to(store.root).as_posix()}
        with store.transaction() as state:
            state["jobs"][job_id].setdefault("postprocess_runs", []).append(record)
        def save_record(**values):
            record.update(values, updated=time.time())
            atomic_json(work / "run.json", record)
            with store.transaction() as state:
                entry = next(r for r in state["jobs"][job_id]["postprocess_runs"] if r["id"] == run_id)
                entry.update(record)
        try:
            save_record()
            reports = []
            inputs = []
            settings = manifest["job"]["settings"]
            expected = list(range(settings["start"], settings["end"] + 1, settings["step"]))
            for index, pair in enumerate(chosen):
                guard()
                images = sorted(groups[pair], key=lambda item: item[0])
                if [number for number, _ in images] != expected:
                    raise ValueError("Beauty連番に重複・不足フレームがあります")
                paths = []
                for number, file in images:
                    guard()
                    report(phase="入力画像を検証中", group=index + 1, groups=len(chosen), layer=pair[0], camera=pair[1], completed=len(paths), total=len(images))
                    path = inside(source, file["path"])
                    if sha256(path) != file["sha256"]:
                        raise ValueError("原本画像が検証記録と一致しません: " + str(path))
                    paths.append(str(path))
                    inputs.append((path, file["sha256"]))
                directory = work / "output" / ("g" + str(len(reports) + 1).zfill(2))
                directory.mkdir(parents=True)
                request = {"input": paths, "output": str(directory), "options": options, "config": config,
                           "fps": fps, "result": str(directory / "result.json")}
                atomic_json(directory / "request.json", request)
                python = config["fastdvdnet_python"] if options["fastdvdnet"] else sys.executable
                with (directory / "post.log").open("w", encoding="utf-8") as log:
                    proc = subprocess.Popen(python_command(python, Path(__file__).with_name("post_entry.py"), [directory / "request.json"]),
                                            cwd=work, env=runtime_env(store.root, work), stdout=log, stderr=subprocess.STDOUT, **command_flags())
                    group = ProcessGroup(proc)
                    try:
                        while proc.poll() is None:
                            guard()
                            event = read_json(directory / "progress.json", {})
                            report(**{**{"phase": "後処理を起動中"}, **event, "group": index + 1, "groups": len(chosen), "layer": pair[0], "camera": pair[1]})
                            time.sleep(.5)
                        if proc.returncode:
                            raise RuntimeError("追加後処理に失敗しました。ログ: " + str(directory / "post.log"))
                    finally:
                        group.stop()
                result = read_json(directory / "result.json", {})
                if not result.get("verified"):
                    raise ValueError("後処理結果の検証が完了していません")
                reports.append({"layer": pair[0], "camera": pair[1], **result})
            guard()
            report(phase="成果物を保存中")
            files = []
            for path in (work / "output").rglob("*"):
                if path.is_file() and (path.suffix in {".png", ".mkv", ".log"} or path.name in {"result.json", "verification.framemd5"}):
                    files.append({"path": path.relative_to(work / "output").as_posix(), "sha256": sha256(path), "bytes": path.stat().st_size})
            for path, expected_hash in inputs:
                if sha256(path) != expected_hash:
                    raise ValueError("原本画像が処理中に変更されました")
            final = inside(store.root, result_rel)
            staging = inside(store.root, "staging/late_post_" + run_id)
            staging.mkdir(parents=True)
            for file in files:
                guard()
                destination = inside(staging, file["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(inside(work / "output", file["path"]), destination)
                if sha256(destination) != file["sha256"]:
                    raise ValueError("後処理成果物のコピーが一致しません")
            atomic_json(staging / "manifest.json", {"verified": True, "job_id": job_id, "source": job["publication"],
                        "options": options, "fps": fps, "groups": reports, "files": files})
            guard()
            final.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(final)
            save_record(state="complete", publication=result_rel)
            report(phase="完了", completed=len(chosen), total=len(chosen))
            return dict(record)
        except Exception as exc:
            save_record(state="cancelled" if isinstance(exc, InterruptedError) else "failed", error=str(exc))
            raise

from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .common import atomic_json, command_flags, digest, inside, read_json, runtime_env, sha256, worker_dir, python_command
from .process import ProcessGroup


def _output_tail(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-2000:]


def check_dependencies(root, config, required=None, log_path=None):
    result = {"ffmpeg": False, "fastdvdnet": False}
    wanted = {'ffmpeg', 'fastdvdnet'} if required is None else set(required)
    env = runtime_env(root)
    repo = Path(config.get("fastdvdnet_repo", ""))
    checks = {
        "ffmpeg": ([config.get("ffmpeg", "")], [config.get("ffmpeg", ""), "-version"], 15),
        "fastdvdnet": ([config.get("fastdvdnet_python", ""), str(repo / "models.py"),
                        str(repo / "fastdvdnet.py"), config.get("fastdvdnet_model", "")],
                       [config.get("fastdvdnet_python", ""), "-c",
                        "import torch,numpy,PIL; print(torch.__version__)"], 60),
    }
    details = {}
    for name in ("ffmpeg", "fastdvdnet"):
        if name not in wanted:
            continue
        paths, command, timeout = checks[name]
        detail = {"timeout_seconds": timeout, "executable": command[0]}
        missing = [path for path in paths if not path or not Path(path).is_file()]
        if missing:
            detail.update(status="missing", missing_paths=missing)
            result.setdefault("dependency_errors", {})[name] = "必要なファイルがありません: " + ", ".join(missing)
        else:
            started = time.monotonic()
            try:
                check = subprocess.run(command, capture_output=True, env=env,
                                       timeout=timeout, **command_flags())
                detail.update(status="ok" if check.returncode == 0 else "exit_nonzero",
                              returncode=check.returncode)
                result[name] = check.returncode == 0
                if check.returncode:
                    detail["stdout_tail"] = _output_tail(check.stdout)
                    detail["stderr_tail"] = _output_tail(check.stderr)
                    result.setdefault("dependency_errors", {})[name] = "終了コード " + str(check.returncode)
            except subprocess.TimeoutExpired as exc:
                detail.update(status="timeout", stdout_tail=_output_tail(exc.stdout),
                              stderr_tail=_output_tail(exc.stderr))
                result.setdefault("dependency_errors", {})[name] = str(exc)
            except OSError as exc:
                detail.update(status="launch_error", error=str(exc))
                result.setdefault("dependency_errors", {})[name] = str(exc)
            finally:
                detail["elapsed_seconds"] = round(time.monotonic() - started, 3)
        details[name] = detail
    if log_path is not None:
        atomic_json(log_path, {"time": time.time(), "required": sorted(wanted), "checks": details})
    return result

def process_outputs(root, work, job, config, heartbeat):
    options = job["settings"].get("post", {})
    if not options.get("video") and not options.get("fastdvdnet"):
        atomic_json(work / "post_manifest.json", {"files": [], "groups": []})
        return
    caps = check_dependencies(root, config, required={key for key, enabled in
                              [('ffmpeg', options.get('video')), ('fastdvdnet', options.get('fastdvdnet'))] if enabled},
                              log_path=work / "logs" / "post_dependencies.json")
    if any(options.get(key) and not caps["ffmpeg" if key == "video" else key] for key in ("video", "fastdvdnet")):
        raise RuntimeError("後処理の依存環境が不足しています。診断ログ: " + str(work / "logs" / "post_dependencies.json"))
    previous = read_json(work / "post_manifest.json")
    if previous and previous.get("signature") == job["signature"] and previous.get("options") == options:
        if all(sha256(inside(work, f["path"])) == f["sha256"] for f in previous["files"]):
            return
    from .results import readable, vacant
    base = inside(work, vacant(work, "processed/p" + time.strftime('%y%m%d_%H%M%S')))
    base.mkdir(parents=True)
    groups = defaultdict(list)
    for frame in read_json(work / "frames.json", {}).values():
        beauty = next(f for f in frame["files"] if f["pass"] == "beauty")
        groups[(frame["layer"], frame["camera"])].append((frame["frame"], str(inside(work, beauty["path"]))))
    reports = []
    for index, ((layer, camera), images) in enumerate(groups.items(), 1):
        # Human-readable layer/camera names remain in the manifest. Keeping
        # transient group folders short avoids MAX_PATH on UNC shares.
        directory = base / ("g" + str(index).zfill(2))
        directory.mkdir()
        request = {"input": [path for _, path in sorted(images)], "output": str(directory),
                   "options": options, "config": config, "fps": job["settings"]["fps"],
                   "result": str(directory / "result.json")}
        atomic_json(directory / "request.json", request)
        python = config["fastdvdnet_python"] if options.get("fastdvdnet") else sys.executable
        with (directory / "post.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(python_command(python, Path(__file__).with_name("post_entry.py"), [directory / "request.json"]),
                env=runtime_env(root, work), cwd=work, stdout=log, stderr=subprocess.STDOUT, **command_flags())
            group = ProcessGroup(proc)
            try:
                while proc.poll() is None:
                    if heartbeat():
                        raise InterruptedError("後処理を停止しました")
                    time.sleep(2)
                if proc.returncode:
                    raise RuntimeError("後処理失敗: " + str(directory / "post.log"))
            finally:
                group.stop()
        report = read_json(directory / "result.json")
        if not report or not report.get("verified"):
            raise ValueError("後処理の検証記録がありません")
        reports.append({"layer": layer, "camera": camera, **report})
    files = [{"path": p.relative_to(work).as_posix(), "sha256": sha256(p), "bytes": p.stat().st_size}
             for p in base.rglob("*") if p.is_file() and p.suffix in {".png", ".mkv"}]
    atomic_json(work / "post_manifest.json", {"signature": job["signature"], "options": options, "files": files, "groups": reports})

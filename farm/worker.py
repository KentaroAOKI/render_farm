from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback
import uuid
import threading

from .assets import probe, stored_job_metadata, project_path_mappings
from .common import (FileLock, atomic_json, command_flags, digest, inside, installations,
                     machine_id, read_json, runtime_env, sha256, worker_dir, maya_env, DosAlias)
from .images import verify_record
from .process import ProcessGroup, terminate_owned
from .store import Store, ACTIVE, OwnershipLostError
from .results import result_path


class Watchdog:
    """Monotonic clock, reset only for a new unit/attempt, never for log/file mtimes."""
    def __init__(self, seconds, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.key, self.since = None, None

    def expired(self, status):
        if status.get("state") != "rendering":
            self.key, self.since = None, None
            return False
        key = (status.get("attempt"), status.get("unit"))
        if self.key != key:
            self.key, self.since = key, self.clock()
        return self.clock() - self.since >= self.seconds


def worker_config(root):
    root = Path(root).resolve()
    config = read_json(worker_dir(root) / "config.json", {"enabled_versions": ["2025", "2026", "2027"], "maya_paths": {}})
    for key in ("ffmpeg", "fastdvdnet_python", "fastdvdnet_repo", "fastdvdnet_model"):
        if config.get(key):
            path = Path(config[key])
            config[key] = str(path if path.is_absolute() else root / path)
    if not config.get("ffmpeg") and (root / "bin/ffmpeg.exe").is_file():
        config["ffmpeg"] = str(root / "bin/ffmpeg.exe")
    repo = root / "tools/fastdvdnet"
    candidates = list((root / "bin/WinPython").glob("*/python/python.exe"))
    if (repo / "models.py").is_file() and (repo / "fastdvdnet.py").is_file() and (repo / "model.pth").is_file():
        if not config.get("fastdvdnet_repo"):
            config["fastdvdnet_repo"] = str(repo)
        if not config.get("fastdvdnet_model"):
            config["fastdvdnet_model"] = str(repo / "model.pth")
        if len(candidates) == 1 and not config.get("fastdvdnet_python"):
            config["fastdvdnet_python"] = str(candidates[0])
    return config


def diagnose(root, config=None):
    config = config or worker_config(root)
    s = Store(root).read()
    available = installations(config.get("maya_paths"))
    result, errors = {}, {}
    for version, path in available.items():
        if version not in config.get("enabled_versions", available):
            continue
        plugins = {"mtoa"}
        for job in s["jobs"].values():
            if job["state"] in {"complete", "deleted"}:
                continue
            if (job["maya_version"] == version or
                    (not job.get("maya_version_required", True) and not job.get("started"))):
                plugins.update(job.get("plugin_versions", job["plugins"]))
        try:
            caps = probe(root, root, "", version, config, mode="capabilities", payload={"plugins": sorted(plugins)})
            if not caps["oiiotool"]:
                raise RuntimeError("Arnold付属oiiotoolがありません")
            caps["path"] = path
            from .post import check_dependencies
            postcaps = check_dependencies(root, config)
            caps.update(postcaps)
            result[version] = caps
        except Exception as exc:
            errors[version] = str(exc)
    atomic_json(worker_dir(root) / "capabilities.json", {"versions": result, "errors": errors, "updated": time.time()})
    return result, errors


def publication(store, job, work):
    frames = read_json(work / "frames.json", {})
    settings = job["settings"]
    expected = len(settings["layers"]) * len(settings["cameras"]) * len(range(settings["start"], settings["end"] + 1, settings["step"]))
    if len(frames) != expected:
        raise ValueError("公開するフレーム数が一致しません")
    expected_units = {digest((layer, camera, frame))[:24]: (layer, camera, frame)
                      for layer in settings["layers"] for camera in settings["cameras"]
                      for frame in range(settings["start"], settings["end"] + 1, settings["step"])}
    if set(frames) != set(expected_units):
        raise ValueError("公開するフレーム・カメラ・レイヤーが一致しません")
    for key, record in frames.items():
        if (record.get("layer"), record.get("camera"), record.get("frame")) != expected_units[key] or record.get("signature") != job["signature"]:
            raise ValueError("フレームのジョブ識別情報が一致しません")
        verify_record(work, record)
    current_record = store.read()["jobs"][job["id"]]
    manifest = {"schema": 1, "job": {k: v for k, v in current_record.items() if k != "token"}, "frames": frames,
                "post": read_json(work / "post_manifest.json", {}), "verified": True}
    version = digest([job["signature"], frames, manifest["post"]])
    staging = inside(store.root, "staging/" + job["id"] + "_" + uuid.uuid4().hex[:16])
    staging.mkdir(parents=True)
    records = [f for frame in frames.values() for f in frame["files"]]
    records += manifest["post"].get("files", [])
    for file in records:
        source = inside(work, file["path"])
        target = inside(staging, file["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha256(target) != file["sha256"]:
            raise ValueError("公開先コピーのハッシュが一致しません")
    if (work / "logs").exists():
        shutil.copytree(work / "logs", staging / "logs")
    atomic_json(staging / "manifest.json", manifest)
    # Promotion and terminal state occur with the same fencing lock.
    with store.transaction() as s:
        current = s["jobs"][job["id"]]
        if current.get("token") != job["token"] or current["state"] not in ACTIVE:
            raise OwnershipLostError("公開の所有権が失効しました")
        if current.get("control"):
            raise InterruptedError("公開前に停止要求を受けました")
        rel = result_path(store, s, job)
        final = inside(store.root, rel)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            old = read_json(final / "manifest.json")
            if not old or old["frames"] != frames:
                raise ValueError("既存成果物と衝突しました")
            for file in records:
                if sha256(inside(final, file["path"])) != file["sha256"]:
                    raise ValueError("既存成果物の検証に失敗しました")
        else:
            staging.rename(final)
        current.update(state="complete", publication=rel, token=None, control=None, updated=time.time(), error=None,
                       completed=len(frames), total=expected)
        current["revision"] += 1
        scene = s["scenes"].get(job["scene_id"])
        if scene and scene["revision"] == job["scene_revision"]:
            scene.update(state="complete", latest=rel)
            atomic_json(final / "latest.json", {"version": version, "path": rel})
    return rel


class Supervisor:
    def __init__(self, root, poll=2, launch=None):
        self.store = Store(root)
        self.root = self.store.root
        self.worker = machine_id()
        self.directory = worker_dir(root)
        self.poll = poll
        self.launch_override = launch
        self.config = worker_config(root)
        self.stopfile = self.directory / "worker_control.json"

    def stopping(self):
        return bool(read_json(self.stopfile, {}).get("stop"))

    def run(self, once=False):
        with FileLock(self.directory / "worker.lock", timeout=0):
            atomic_json(self.stopfile, {"stop": False})
            self.store.worker_status(self.worker, state="diagnosing", pid=os.getpid())
            caps, errors = diagnose(self.root, self.config)
            if not caps:
                self.store.worker_status(self.worker, state="error", error=errors or "対応Mayaがありません")
                return
            self.store.worker_status(self.worker, state="idle", capabilities=caps, error=errors, pid=os.getpid())
            last_signature = None
            try:
                while not self.stopping():
                    state = self.store.read()
                    fresh_config = worker_config(self.root)
                    config_changed = fresh_config != self.config
                    self.config = fresh_config
                    pending_signature = digest(sorted((j["maya_version"], j.get("maya_version_required", True),
                                                       sorted(j.get("plugin_versions", j["plugins"]).items()),
                                                       sorted(j["settings"].get("post", {}).items()), str(j["settings"].get("device", "")))
                                                      for j in state["jobs"].values() if j["state"] == "pending"))
                    if config_changed or (last_signature is not None and pending_signature != last_signature):
                        self.store.worker_status(self.worker, state="diagnosing", job=None, pid=os.getpid())
                        caps, errors = diagnose(self.root, self.config)
                        self.store.worker_status(self.worker, state="idle", capabilities=caps, error=errors, job=None, pid=os.getpid())
                    last_signature = pending_signature
                    # Only the same machine may automatically resume its orphaned claim.
                    # Kill-on-close job objects ensure its old Maya did not survive the supervisor.
                    job = next((copy.deepcopy(j) for j in state["jobs"].values() if j.get("owner") == self.worker and j["state"] in ACTIVE), None)
                    if job and job["maya_version"] not in caps:
                        self.store.worker_status(self.worker, state="error", error="所有中ジョブのMaya環境がありません")
                        break
                    job = job or self.store.claim(self.worker, caps)
                    if job:
                        self.store.worker_status(self.worker, state="working", job=job["id"])
                        with FileLock(self.root / "data" / ("run_" + job["id"] + ".lock"), timeout=0):
                            self.run_job(job, caps[job["maya_version"]])
                    elif once:
                        break
                    else:
                        self.store.worker_status(self.worker, state="idle", capabilities=caps, job=None)
                        time.sleep(min(5, self.poll))
                    if once:
                        break
            finally:
                self.store.worker_status(self.worker, state="stopped", job=None)

    def run_job(self, job, caps):
        stopped = threading.Event()
        def keepalive():
            while not stopped.wait(10):
                try:
                    # Do not change phase/progress here: the render/publish loop owns them.
                    self.store.heartbeat(job["id"], job["token"])
                except OwnershipLostError:
                    return
                except (OSError, ValueError):
                    pass
        thread = threading.Thread(target=keepalive, name="farm-heartbeat", daemon=True)
        thread.start()
        try:
            return self._run_with_io_retry(job, caps)
        finally:
            stopped.set()
            thread.join(timeout=1)

    def _run_with_io_retry(self, job, caps):
        def ownership_lost(error):
            # Never finish/reset a job using a rejected token, or steal a claim
            # from another PC. The normal queue loop reads fresh ownership.
            atomic_json(self.directory / "ownership_lost.json",
                        {"job": job["id"], "error": str(error), "updated": time.time()})
            return "ownership_lost"
        while True:
            try:
                return self._run_job(job, caps)
            except OwnershipLostError as exc:
                return ownership_lost(exc)
            except OSError as exc:
                # Keep the claim through a disconnected share or a full destination.
                # Restart this same attempt from its checkpoints only after storage returns.
                message = str(exc)
                while True:
                    time.sleep(5)
                    try:
                        current = self.store.owned(job["id"], job["token"])
                        if current.get("control") or self.stopping():
                            return self.store.finish(job["id"], job["token"], "paused")
                        self.store.heartbeat(job["id"], job["token"], state="waiting_io", error=message)
                        work = inside(self.root, current.get("work", "workers/" + self.worker))
                        frames = read_json(work / "frames.json", {})
                        required = 1024 * 1024 + sum(f.get("bytes", 0) for frame in frames.values() for f in frame["files"])
                        if shutil.disk_usage(self.root).free < required:
                            continue
                        job = current
                        self.store.event(job["id"], "保存先の回復を確認。検証済みフレームから再開します")
                        break
                    except OwnershipLostError as exc:
                        return ownership_lost(exc)
                    except (OSError, TimeoutError):
                        continue

    def _run_job(self, job, caps):
        # A paused job may have been moved from another PC/drive. Preserve its relative work path.
        relative = job.get("work") or ("workers/" + self.worker + "/jobs/" + job["id"])
        work = inside(self.root, relative)
        work.mkdir(parents=True, exist_ok=True)
        self.store.heartbeat(job["id"], job["token"], work=relative)
        alias = DosAlias(self.root, work / "dos_alias.json")
        try:
            alias_root = alias.__enter__()
            maya_work = Path(alias_root) / relative
            state = self.store.read()
            scene = state["scenes"].get(job["scene_id"])
            source = job.get("source")
            if not source:
                if not scene:
                    raise ValueError("登録シーンがありません。プロジェクトを登録し直してください")
                source = {"project_id": scene["project_id"], "relative": scene["relative"], "metadata": stored_job_metadata(scene)}
                # Preserve the original selected path for jobs created before direct mode.
                if job.get("snapshot"):
                    previous = read_json(inside(self.root, job["snapshot"]))
                    if previous:
                        source["relative"] = Path(previous["scene"]).relative_to(previous["project"]).as_posix()
                        source["metadata"] = previous["metadata"]
            project = state["projects"][source["project_id"]]
            project_path = self.store.project_path(project)
            scene_path = inside(project_path, source["relative"])
            mappings = [[a, str(inside(self.root, b))] for a, b in project.get("portable", {}).get("mappings", [])]
            known_roots = {a.replace("\\", "/").casefold() for a, b in mappings}
            mappings.extend(pair for pair in project_path_mappings(project_path, scene_path)
                            if pair[0].casefold() not in known_roots)
            restarts = dict(job.get("restarts", {}))
            while True:
                self.store.owned(job["id"], job["token"])
                if self.stopping():
                    return self.store.finish(job["id"], job["token"], "paused")
                attempt = uuid.uuid4().hex[:16]
                request = {"root": str(self.root), "work": str(maya_work),
                    "project": str(project_path), "scene": str(scene_path), "mappings": mappings,
                    "portable": bool(project.get("portable")),
                    "job": job, "metadata": source["metadata"], "attempt": attempt, "oiiotool": caps["oiiotool"]}
                atomic_json(work / "request.json", request)
                atomic_json(work / "control.json", {"stop": False})
                env = maya_env(self.root, work, caps["path"])
                if alias.drive:
                    env = {key: value.replace(str(self.root), str(alias_root).rstrip("\\")) for key, value in env.items()}
                env["PORTABLE_FARM_REQUEST"] = str(maya_work / "request.json")
                self.store.heartbeat(job["id"], job["token"], state="opening", attempt=attempt, restarts=restarts)
                logpath = work / "logs" / ("maya_" + attempt + ".log")
                logpath.parent.mkdir(exist_ok=True)
                command = [str(Path(caps["path"]) / "bin/maya.exe"), "-log", "logs/" + logpath.name,
                           "-command", 'python("import farm.maya_entry; farm.maya_entry.gui_start()")']
                if self.launch_override:
                    command = self.launch_override(request)
                with (logpath.with_suffix(".stdout.log")).open("w", encoding="utf-8") as out:
                    proc = subprocess.Popen(command, cwd=maya_work, env=env, stdout=out, stderr=subprocess.STDOUT)
                    group = ProcessGroup(proc)
                    try:
                        result, unit = self.monitor(job, work, proc, group, attempt, restarts)
                    finally:
                        group.stop()
                if result == "timeout":
                    if restarts.get(unit, 0) >= job["settings"]["restart_limit"]:
                        raise RuntimeError("同じフレームのMaya再起動上限に達しました")
                    restarts[unit] = restarts.get(unit, 0) + 1
                    self.store.event(job["id"], "フレーム " + unit + " が時間超過。Maya再起動 " + str(restarts[unit]) + " 回目")
                    self.store.heartbeat(job["id"], job["token"], state="restarting", restarts=restarts)
                    continue
                if result == "io_restart":
                    self.store.event(job["id"], "保存先の切断後に終了したMayaを再起動します")
                    continue
                if result == "paused":
                    return self.store.finish(job["id"], job["token"], "paused")
                break
            self.store.heartbeat(job["id"], job["token"], state="postprocessing")
            from .post import process_outputs
            process_outputs(self.root, work, job, self.config,
                            lambda: self.store.heartbeat(job["id"], job["token"], state="postprocessing") or self.stopping())
            self.store.heartbeat(job["id"], job["token"], state="publishing")
            publication(self.store, job, work)
        except InterruptedError:
            self.store.finish(job["id"], job["token"], "paused")
        except OSError:
            raise
        except Exception:
            error = traceback.format_exc()
            atomic_json(work / "failure.json", {"error": error, "time": time.time()})
            self.store.finish(job["id"], job["token"], "failed", error=error)
        finally:
            alias.__exit__()

    def monitor(self, job, work, proc, group, attempt, restarts):
        watchdog = Watchdog(job["settings"]["timeout_minutes"] * 60)
        start = time.monotonic()
        stop_since = None
        disconnected = False
        while True:
            try:
                status = read_json(work / "maya_status.json", {})
                if status.get("attempt") != attempt:
                    status = {"state": "opening"}
                phase = status.get("state", "opening")
                control = self.store.heartbeat(job["id"], job["token"],
                    state=phase if phase in ACTIVE else "rendering", completed=status.get("completed", 0),
                    total=status.get("total", job.get("total", 0)), unit=status.get("unit"), elapsed=time.monotonic() - start,
                    maya_pid=proc.pid, restarts=restarts, loading_started=status.get("loading_started"),
                    loading_seconds=status.get("loading_seconds"), max_frame_seconds=status.get("max_frame_seconds"))
                if control or self.stopping():
                    atomic_json(work / "control.json", {"stop": True})
                    if stop_since is None:
                        stop_since = time.monotonic()
                    # Delete is prompt; normal stop waits for a frame or its watchdog timeout.
                    if control == "delete" and time.monotonic() - stop_since >= 5:
                        group.stop()
                        return "paused", None
                    if phase in {"opening", "waiting_io"} and time.monotonic() - stop_since >= 5:
                        group.stop()
                        return "paused", None
                if phase == "error":
                    raise RuntimeError(status.get("error", "Maya worker failed"))
                if phase in {"render_complete", "paused"}:
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        group.stop()
                    return ("paused" if phase == "paused" or control or self.stopping() else "complete"), None
                if watchdog.expired(status):
                    group.stop()
                    return ("paused", None) if stop_since else ("timeout", status.get("unit", "unknown"))
                if proc.poll() is not None:
                    final_status = read_json(work / "maya_status.json", {})
                    if final_status.get("attempt") == attempt and final_status.get("state") in {"render_complete", "paused", "error"}:
                        # The process can finish between the earlier status read and poll().
                        continue
                    if disconnected:
                        return "io_restart", None
                    raise RuntimeError("Mayaが完了記録を残さず終了しました。ログを確認してください")
                if phase == "opening" and time.monotonic() - start >= 3600:
                    raise RuntimeError("Maya起動・シーン読み込みが60分で応答しませんでした")
                time.sleep(self.poll)
            except OwnershipLostError:
                # The caller closes only this attempt's owned process group.
                # A rejected token cannot be repaired by waiting for storage.
                raise
            except (OSError, TimeoutError):
                # Do not classify a disconnected disk as a frozen renderer. The Maya guard
                # also requires the shared ownership record before starting its next frame.
                watchdog = Watchdog(job["settings"]["timeout_minutes"] * 60)
                disconnected = True
                time.sleep(5)

from __future__ import annotations

from contextlib import contextmanager
import copy
from pathlib import Path
import time
import uuid

from .common import FileLock, atomic_json, read_json, inside, digest

ACTIVE = {"claimed", "opening", "rendering", "validating", "postprocessing", "publishing", "restarting", "waiting_io"}
TERMINAL = {"complete", "failed", "deleted"}


class OwnershipLostError(PermissionError):
    """A rejected ownership token, distinct from filesystem permission failures."""


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "data/state.json"
        self.lockpath = self.root / "data/state.lock"
        with FileLock(self.lockpath):
            if not self.path.exists():
                atomic_json(self.path, {"schema": 1, "farm_id": uuid.uuid4().hex, "revision": 0,
                    "settings": {"timeout_minutes": 30, "restart_limit": 3, "scan_seconds": 30},
                    "projects": {}, "scenes": {}, "jobs": {}, "workers": {}})

    def read(self):
        state = read_json(self.path)
        if state is None:
            raise FileNotFoundError(str(self.path))
        if state.get("schema") != 1:
            raise ValueError("未対応または破損した状態ファイルです")
        return state

    @contextmanager
    def transaction(self):
        with FileLock(self.lockpath):
            state = self.read()
            yield state
            state["revision"] += 1
            atomic_json(self.path, state)

    def add_project(self, path, version, name=None, extras=None):
        from .common import SUPPORTED
        path = Path(path).resolve()
        if version not in SUPPORTED or not path.is_dir():
            raise ValueError("Mayaバージョンまたはプロジェクトが不正です")
        if self.root == path or self.root.is_relative_to(path):
            raise ValueError("プロジェクト内にファーム自身を配置することはできません")
        pid = digest(str(path).casefold() + version)[:20]
        location = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)
        with self.transaction() as s:
            # Registration is idempotent, including after a portable root has moved.
            for existing_id, existing in s["projects"].items():
                if existing["maya_version"] == version and self.project_path(existing) == path:
                    return existing_id
            for archived_id, archive in list(s.get("project_history", {}).items()):
                existing = archive["project"]
                if existing["maya_version"] == version and self.project_path(existing) == path:
                    s["projects"][archived_id] = existing
                    s["scenes"].update(archive["scenes"])
                    del s["project_history"][archived_id]
                    return archived_id
            s["projects"][pid] = {"id": pid, "name": name or path.name, "path": location,
                "maya_version": version, "extras": [str(Path(x).resolve()) for x in (extras or [])], "error": None}
        return pid

    def remove_project(self, pid, expected_project=None):
        """Unregister without deleting sources, snapshots, output, or job history."""
        with FileLock(self.root / "data" / ("scan_" + pid + ".lock"), timeout=0):
            with self.transaction() as s:
                project = s["projects"].get(pid)
                if project is None:
                    raise ValueError("このプロジェクトは既に登録解除されています。一覧を更新してください")
                if expected_project is not None and project != expected_project:
                    raise ValueError("プロジェクト情報が変更されました。一覧を更新してから登録解除してください")
                scenes = {sid: scene for sid, scene in s["scenes"].items() if scene["project_id"] == pid}
                jobs = [job for job in s["jobs"].values() if job["scene_id"] in scenes]
                if any(job["state"] in ACTIVE for job in jobs):
                    raise ValueError("実行中のジョブがあります。完了または停止を確認してから登録解除してください")
                now = time.time()
                canceled = 0
                for job in jobs:
                    if job["state"] in {"pending", "held", "paused"}:
                        job.update(state="deleted", updated=now, control=None, token=None,
                                   error="プロジェクトの登録解除により取り消しました")
                        job["revision"] += 1
                        job["events"].append({"time": now, "message": "プロジェクト登録解除"})
                        canceled += 1
                for scene in scenes.values():
                    if scene["state"] == "queued":
                        scene["state"] = "unrendered"
                s.setdefault("project_history", {})[pid] = {"project": project, "scenes": scenes, "removed": now}
                del s["projects"][pid]
                for sid in scenes:
                    del s["scenes"][sid]
                return {"project": project["name"], "scenes": len(scenes), "canceled_jobs": canceled}

    def project_path(self, project):
        p = Path(project["path"])
        return p.resolve() if p.is_absolute() else inside(self.root, p)

    def enqueue_record(self, scene_id, expected_revision, snapshot, settings, source=None, maya_version=None):
        from .common import SUPPORTED
        if maya_version is not None and maya_version not in SUPPORTED:
            raise ValueError("対応していないMayaバージョンです")
        with self.transaction() as s:
            scene = s["scenes"][scene_id]
            if scene.get("revision") != expected_revision or scene.get("state") in {"missing", "error"}:
                raise ValueError("シーンが更新されました。一覧を更新してください")
            settings = copy.deepcopy(settings)
            signature = digest([scene_id, expected_revision, settings, source, maya_version])
            for job in s["jobs"].values():
                if job["signature"] == signature and job["state"] not in TERMINAL:
                    return job["id"]
            # Existing IDs/workspaces remain unchanged. New IDs are short enough
            # to leave room for Maya/postprocessing paths on Windows UNC shares.
            jid = uuid.uuid4().hex[:16]
            while jid in s["jobs"]:
                jid = uuid.uuid4().hex[:16]
            plugin_versions = copy.deepcopy(scene["metadata"].get("plugins", {}))
            s["jobs"][jid] = {"id": jid, "name": scene["name"], "scene_id": scene_id,
                "scene_revision": expected_revision, "signature": signature, "snapshot": snapshot,
                "settings": settings, "maya_version": maya_version or scene["maya_version"],
                "maya_version_required": maya_version is not None,
                "scene_maya_version": scene["maya_version"],
                "plugins": {key: value for key, value in plugin_versions.items() if key != "mtoa"},
                "plugin_versions": plugin_versions, "state": "pending", "revision": 0,
                "order": max([j["order"] for j in s["jobs"].values()] or [0]) + 1,
                "created": time.time(), "updated": time.time(), "owner": None, "token": None,
                "completed": 0, "total": len(settings["layers"]) * len(settings["cameras"]) * len(range(settings["start"], settings["end"] + 1, settings["step"])),
                "restarts": {}, "error": None, "events": [], "control": None}
            if source:
                s["jobs"][jid].update(source=copy.deepcopy(source), source_mode="direct")
            scene["state"] = "queued"
        return jid

    def claim(self, worker, capabilities):
        package = read_json(self.root / "package_status.json")
        if package is not None and not package.get("ready"):
            raise RuntimeError("USB梱包が未完了または検証に失敗しています")
        with self.transaction() as s:
            for job in sorted(s["jobs"].values(), key=lambda j: (j["order"], j["created"])):
                if job["state"] != "pending":
                    continue
                # Once work has started, keep the same Maya version so validated
                # frames from different Maya releases cannot be mixed on resume.
                if job.get("maya_version_required", True) or job.get("started") or job.get("work"):
                    versions = [job["maya_version"]]
                else:
                    preferred = job.get("scene_maya_version", job["maya_version"])
                    versions = [preferred] + [v for v in sorted(capabilities, reverse=True) if v != preferred]
                # Plugin presence is required, but its version is informational.
                required = job.get("plugin_versions", job.get("plugins", {}))
                selected = None
                for version in versions:
                    caps = capabilities.get(version)
                    if caps is None:
                        continue
                    plugins = caps.get("plugins", {})
                    if "mtoa" not in plugins or any(key not in plugins for key in required):
                        continue
                    if job["settings"].get("device") == "GPU" and not caps.get("gpu"):
                        continue
                    if job["settings"].get("post", {}).get("fastdvdnet") and not caps.get("fastdvdnet"):
                        continue
                    if job["settings"].get("post", {}).get("video") and not caps.get("ffmpeg"):
                        continue
                    selected = version
                    break
                if selected is None:
                    continue
                scene = s["scenes"].get(job["scene_id"], {})
                if scene.get("revision") != job["scene_revision"] or scene.get("state") == "missing":
                    job.update(state="held", error="元シーンが更新されました")
                    continue
                job.update(state="claimed", owner=worker, token=uuid.uuid4().hex,
                           maya_version=selected,
                           started=time.time(), updated=time.time(), control=None)
                job["revision"] += 1
                return copy.deepcopy(job)
        return None

    def owned(self, jid, token):
        job = self.read()["jobs"][jid]
        if not token or job.get("token") != token or job["state"] not in ACTIVE:
            raise OwnershipLostError("ジョブの所有権が無効です")
        return job

    def heartbeat(self, jid, token, **values):
        with self.transaction() as s:
            job = s["jobs"][jid]
            if not token or job.get("token") != token or job["state"] not in ACTIVE:
                raise OwnershipLostError("ジョブの所有権が無効です")
            if "state" in values and values["state"] not in ACTIVE:
                raise ValueError("非アクティブ状態にはfinishを使用してください")
            allowed = {"state", "completed", "total", "unit", "elapsed", "work", "error", "restarts", "maya_pid", "attempt"}
            job.update({k: v for k, v in values.items() if k in allowed})
            job["updated"] = time.time()
            if job.get("owner") in s["workers"]:
                s["workers"][job["owner"]].update(updated=time.time(), state="working", job=jid)
            # Progress does not change the operator-command revision.
            return job.get("control")

    def finish(self, jid, token, state, error=None, publication=None):
        if state not in TERMINAL | {"paused", "held"}:
            raise ValueError(state)
        with self.transaction() as s:
            job = s["jobs"][jid]
            if not token or job.get("token") != token:
                raise OwnershipLostError("ジョブの所有権が無効です")
            if job.get("control") == "delete":
                state = "deleted"
            job.update(state=state, error=error, updated=time.time(), token=None, control=None)
            job["revision"] += 1
            if publication:
                job["publication"] = publication
            scene = s["scenes"].get(job["scene_id"])
            if scene and scene["revision"] == job["scene_revision"]:
                scene["state"] = "complete" if state == "complete" else "unrendered"
                if publication:
                    scene["latest"] = publication

    def action(self, jid, action, expected_revision):
        with self.transaction() as s:
            job = s["jobs"][jid]
            if job["revision"] != expected_revision:
                raise ValueError("他の操作で変更されました。一覧を更新してください")
            if action in {"delete", "hold", "stop"}:
                if job["state"] in ACTIVE:
                    job["control"] = "delete" if action == "delete" else "pause"
                else:
                    job["state"] = "deleted" if action == "delete" else "held"
            elif action in {"resume", "retry"}:
                scene = s["scenes"].get(job["scene_id"], {})
                if scene.get("revision") != job["scene_revision"] or scene.get("state") in {"missing", "error"}:
                    raise ValueError("旧版ジョブです。最新版を投入してください")
                if job["state"] not in {"held", "paused", "failed"}:
                    raise ValueError("再開できる状態ではありません")
                job.update(state="pending", error=None, control=None)
                if action == "retry":
                    job["restarts"] = {}
            elif action in {"up", "down"}:
                pending = sorted((j for j in s["jobs"].values() if j["state"] in {"pending", "held", "paused"}), key=lambda j: j["order"])
                ids = [j["id"] for j in pending]
                if jid not in ids:
                    raise ValueError("待機中ジョブだけを並べ替えできます")
                index = ids.index(jid)
                other = index + (-1 if action == "up" else 1)
                if 0 <= other < len(pending):
                    job["order"], pending[other]["order"] = pending[other]["order"], job["order"]
                    pending[other]["revision"] += 1
            else:
                raise ValueError(action)
            job["revision"] += 1
            job["updated"] = time.time()

    def update_job_post(self, jid, options, expected_revision):
        """Edit only postprocessing; preserve the render signature/checkpoints."""
        if set(options) != {"video", "fastdvdnet"} or any(type(v) is not bool for v in options.values()):
            raise ValueError("後処理設定が不正です")
        with self.transaction() as s:
            job = s["jobs"][jid]
            if job["revision"] != expected_revision:
                raise ValueError("他の操作で変更されました。一覧を更新してください")
            if job["state"] not in {"pending", "held", "paused", "failed"}:
                raise ValueError("待機・保留・停止・エラーのジョブだけ変更できます。実行中は停止を確認してください")
            old = copy.deepcopy(job["settings"].get("post", {}))
            job["settings"]["post"] = dict(options)
            job["revision"] += 1
            job["updated"] = time.time()
            job["events"].append({"time": time.time(), "message": "後処理設定を変更", "before": old, "after": dict(options)})
            return copy.deepcopy(job)

    def event(self, jid, message):
        with self.transaction() as s:
            s["jobs"][jid]["events"].append({"time": time.time(), "message": message})

    def worker_status(self, worker, **values):
        with self.transaction() as s:
            s["workers"].setdefault(worker, {}).update(values, updated=time.time())

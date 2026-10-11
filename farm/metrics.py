"""Read display metrics without modifying queue ownership or job state."""
from .common import inside, read_json


def worker_unresponsive(worker, now, timeout=45):
    """A stale heartbeat is an observation, not permission to reassign work."""
    if not worker or worker.get("state") == "stopped":
        return not worker
    updated = worker.get("updated")
    return updated is None or now - updated > timeout


def display_metrics(root, job):
    result = dict(job)
    if not job.get("work") or not job.get("attempt"):
        return result
    try:
        status = read_json(inside(root, job["work"]) / "maya_status.json", {})
        if status.get("attempt") != job["attempt"]:
            return result
        for key in ("loading_started", "loading_seconds", "max_frame_seconds", "frame", "layer", "camera", "frame_started"):
            if status.get(key) is not None:
                result[key] = status[key]
        result["maya_phase"] = status.get("phase", "")
    except (OSError, ValueError, TimeoutError):
        # A disconnected share must not hide the last stored queue values.
        pass
    return result

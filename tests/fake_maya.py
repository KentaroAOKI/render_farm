"""Controlled subprocess for supervisor recovery tests; never invokes Maya."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import time
from farm.common import atomic_json, read_json, sha256, digest

request = read_json(sys.argv[1])
work = Path(request["work"])
case = sys.argv[2]
counter = read_json(work / "fake_starts.json", {"count": 0})
counter["count"] += 1
atomic_json(work / "fake_starts.json", counter)
unit = digest(("layer", "camera", 1))[:24]
status = {"attempt": request["attempt"], "unit": unit, "state": "rendering", "completed": 0, "total": 1}
atomic_json(work / "maya_status.json", status)
if case == "always_freeze" or (case == "recover" and counter["count"] == 1):
    # Constantly updated log and a partial image must not reset the watchdog.
    while True:
        (work / "fake.log").write_text(str(time.time()))
        time.sleep(.03)
if case == "error":
    status.update(state="error", error="controlled failure")
else:
    image = work / "raw" / unit / "beauty.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    import base64
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
    record = {"unit": unit, "layer": "layer", "camera": "camera", "frame": 1,
              "signature": request["job"]["signature"], "seconds": .01,
              "files": [{"path": "raw/" + unit + "/beauty.png", "pass": "beauty", "sha256": sha256(image), "bytes": image.stat().st_size,
                         "width": 1, "height": 1, "channels": ["R", "G", "B"]}]}
    atomic_json(work / "frames.json", {unit: record})
    status.update(state="render_complete", completed=1)
atomic_json(work / "maya_status.json", status)

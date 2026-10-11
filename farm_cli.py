from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Portable Maya render farm")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gui")
    sub.add_parser("status")
    sub.add_parser("diagnose")
    preview = sub.add_parser("results-dashboard", help="Generate thumbnail HTML and a captioned reel from results videos/image sequences")
    preview.add_argument("--fps", default="24", help="FPS of external image sequences (default: 24)")
    demo = sub.add_parser("demo", help="Create and enqueue two small original test scenes")
    demo.add_argument("--maya", choices=["2025", "2026", "2027"], required=True)
    add = sub.add_parser("add-project")
    add.add_argument("path", type=Path)
    add.add_argument("--maya", choices=["2025", "2026", "2027"], required=True)
    add.add_argument("--extra", action="append", default=[])
    remove = sub.add_parser("remove-project", help="Unregister a project; preserve files and job history")
    remove.add_argument("project")
    scan = sub.add_parser("scan")
    scan.add_argument("--project")
    scan.add_argument("--force", action="store_true", help="保存情報を使わず採用シーンの情報を再取得")
    enqueue_parser = sub.add_parser("enqueue")
    enqueue_parser.add_argument("scene")
    enqueue_parser.add_argument("--settings", type=Path)
    enqueue_parser.add_argument("--maya-version", choices=["2025", "2026", "2027"],
                                help="Mayaのバージョンを固定する。省略時は指定しない")
    action = sub.add_parser("job")
    action.add_argument("id")
    action.add_argument("action", choices=["delete", "hold", "stop", "resume", "retry", "up", "down"])
    action.add_argument("--revision", type=int, required=True)
    edit = sub.add_parser("job-post", help="Edit postprocessing of an inactive job; preserve rendered images")
    edit.add_argument("id")
    edit.add_argument("--revision", type=int, required=True)
    edit.add_argument("--video", choices=["on", "off"], required=True)
    edit.add_argument("--fastdvdnet", choices=["on", "off"], required=True)
    work = sub.add_parser("worker")
    work.add_argument("--once", action="store_true")
    sub.add_parser("stop")
    pack = sub.add_parser("pack")
    pack.add_argument("destination", type=Path)
    pack.add_argument("--scene", action="append", required=True)
    late = sub.add_parser("postprocess", help="Create video/denoised output from a published job without rerendering")
    late.add_argument("job_id")
    late.add_argument("--video", action="store_true")
    late.add_argument("--fastdvdnet", action="store_true")
    late.add_argument("--fps")
    args = parser.parse_args()
    from farm.common import runtime_env, atomic_json, worker_dir, read_json
    os.environ.update(runtime_env(args.root))
    from farm.store import Store
    from farm.worker import worker_config
    store = Store(args.root)
    if args.command in {"gui", "worker", "postprocess"}:
        from farm.results import migrate_results
        migrate_results(store)
    result = None
    if args.command == "gui":
        from farm.gui import run
        return run(args.root)
    if args.command == "status":
        result = store.read()
    elif args.command == "add-project":
        result = store.add_project(args.path, args.maya, extras=args.extra)
    elif args.command == "remove-project":
        result = store.remove_project(args.project)
    elif args.command == "scan":
        from farm.assets import refresh_scene_list as refresh_project
        result = {pid: refresh_project(store, pid, worker_config(args.root), force=args.force) for pid in ([args.project] if args.project else store.read()["projects"])}
    elif args.command == "enqueue":
        from farm.assets import enqueue
        result = enqueue(store, args.scene, read_json(args.settings) if args.settings else {},
                         maya_version=args.maya_version)
    elif args.command == "job":
        store.action(args.id, args.action, args.revision)
    elif args.command == "job-post":
        result = store.update_job_post(args.id, {"video": args.video == "on", "fastdvdnet": args.fastdvdnet == "on"}, args.revision)
    elif args.command == "worker":
        from farm.worker import Supervisor
        Supervisor(args.root).run(once=args.once)
    elif args.command == "stop":
        atomic_json(worker_dir(args.root) / "worker_control.json", {"stop": True})
    elif args.command == "diagnose":
        from farm.worker import diagnose
        result = diagnose(args.root)
    elif args.command == "pack":
        from farm.package import export_package
        result = export_package(store, args.destination, args.scene, worker_config(args.root))
    elif args.command == "postprocess":
        from farm.late_post import run_late_postprocess
        result = run_late_postprocess(store, args.job_id, {"video": args.video, "fastdvdnet": args.fastdvdnet}, worker_config(args.root), fps=args.fps)
    elif args.command == "results-dashboard":
        from farm.results_dashboard import build_dashboard
        result = build_dashboard(store, {**worker_config(args.root), 'sequence_fps': args.fps})
    elif args.command == "demo":
        import uuid
        import shutil
        import time
        from farm.assets import probe, refresh_scene_list as refresh_project, enqueue
        project = store.root / "examples" / ("demo_" + args.maya + "_" + uuid.uuid4().hex[:8])
        probe(store.root, project, "", args.maya, worker_config(store.root), mode="make_fixture", payload={"aov": True, "layers": True})
        shutil.copyfile(project / "scenes/test.ma", project / "scenes/test_second.ma")
        time.sleep(2.1)
        pid = store.add_project(project, args.maya)
        refresh_project(store, pid, worker_config(store.root))
        result = {"project": str(project), "jobs": [enqueue(store, s["id"]) for s in store.read()["scenes"].values() if s["project_id"] == pid]}
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.exit(1)

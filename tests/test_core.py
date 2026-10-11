from __future__ import annotations

import copy
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
import subprocess
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.common import FileLock, atomic_json, digest, inside, read_json, sha256
from farm.store import Store
from farm.assets import refresh_project, enqueue, materialize, validate_settings, ensure_detailed, create_snapshot
from farm.worker import Watchdog, publication, diagnose
from farm.catalog import latest_scenes

BASE = Path(__file__).resolve().parent / "runtime"


def metadata():
    return {"settings": {"start": 1, "end": 2, "step": 1, "width": 64, "height": 64,
        "fps": "24", "format": "png", "device": "CPU", "aa": 1, "adaptive": False,
        "cameras": ["camera"], "layers": ["layer"], "aovs": [], "oidn": "scene"},
        "cameras": [{"name": "camera"}], "layers": [{"name": "layer"}], "aovs": [],
        "plugins": {"mtoa": "test"}, "dependencies": [], "missing": []}


def inspector(root, project, scene, version, config=None, **kwargs):
    result = metadata()
    asset = Path(project) / "texture.bin"
    if asset.exists():
        result["dependencies"] = [str(asset)]
    return result


def stable_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.utime(path, (time.time() - 10, time.time() - 10))


def concurrent_claim(root, gate, results, index):
    sys.dont_write_bytecode = True
    gate.wait()
    result = Store(root).claim("worker" + str(index), {"2027": {"plugins": {"mtoa": "test"}}})
    results.put(result["id"] if result else None)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.base = BASE / ("unit_" + uuid.uuid4().hex)
        self.project = self.base / "日本語 project"
        stable_write(self.project / "scenes" / "a.ma", "test scene")
        self.store = Store(self.base / "farm")
        self.pid = self.store.add_project(self.project, "2027")
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        self.sid = next(iter(self.store.read()["scenes"]))
        ensure_detailed(self.store, [self.sid], inspect_fn=inspector)

    def job(self, **settings):
        scene = self.store.read()["scenes"][self.sid]
        if scene.get("metadata", {}).get("inspection") == "lightweight":
            ensure_detailed(self.store, [self.sid], inspect_fn=inspector)
        return enqueue(self.store, self.sid, settings, prepared_revision=self.store.read()["scenes"][self.sid]["revision"])

    def test_unregister_preserves_files_and_job_history(self):
        jid = self.job()
        snapshot = self.store.read()["jobs"][jid]["snapshot"]
        result = self.store.remove_project(self.pid)
        state = self.store.read()
        self.assertEqual(1, result["canceled_jobs"])
        self.assertNotIn(self.pid, state["projects"])
        self.assertNotIn(self.sid, state["scenes"])
        self.assertIn(self.sid, state["project_history"][self.pid]["scenes"])
        self.assertEqual("deleted", state["jobs"][jid]["state"])
        self.assertTrue((self.project / "scenes/a.ma").is_file())
        self.assertIsNone(snapshot)
        self.assertTrue((self.project / "scenes/a.ma").is_file())
        self.assertIsNone(self.claim())
        with self.assertRaises(ValueError):
            self.store.action(jid, "retry", state["jobs"][jid]["revision"])
        self.assertEqual(self.pid, self.store.add_project(self.project, "2027"))
        restored = self.store.read()
        self.assertIn(self.sid, restored["scenes"])
        self.assertEqual("deleted", restored["jobs"][jid]["state"])
        self.assertEqual("unrendered", restored["scenes"][self.sid]["state"])

    def test_unregister_rejects_active_job(self):
        self.job()
        self.claim()
        before = self.store.read()
        with self.assertRaisesRegex(ValueError, "実行中"):
            self.store.remove_project(self.pid)
        self.assertEqual(before, self.store.read())

    def test_unregister_rejects_scan_lock(self):
        with FileLock(self.store.root / "data" / ("scan_" + self.pid + ".lock")):
            with self.assertRaises(TimeoutError):
                self.store.remove_project(self.pid)
        self.assertIn(self.pid, self.store.read()["projects"])

    def test_unregister_rejects_stale_project(self):
        before = self.store.read()["projects"][self.pid]
        with self.store.transaction() as state:
            state["projects"][self.pid]["extras"] = [str(self.base / "added")]
        with self.assertRaisesRegex(ValueError, "変更"):
            self.store.remove_project(self.pid, expected_project=before)
        self.assertIn(self.pid, self.store.read()["projects"])

    def test_lightweight_requires_full_inspection_before_snapshot(self):
        def inspect(root, project, scene, version, config=None, payload=None):
            result = inspector(root, project, scene, version, config)
            result["inspection"] = "lightweight" if payload["lightweight"] else "full"
            result["references"] = {"assetRN": {"loaded": not payload["lightweight"], "path": "asset.ma"}}
            return result
        stable_write(self.project / "scenes/a.ma", "updated")
        refresh_project(self.store, self.pid, inspect_fn=inspect)
        light = self.store.read()["scenes"][self.sid]
        with self.assertRaisesRegex(ValueError, "軽量調査"):
            create_snapshot(self.store, light)
        ensure_detailed(self.store, [self.sid], inspect_fn=inspect)
        full = self.store.read()["scenes"][self.sid]
        self.assertEqual("full", full["metadata"]["inspection"])
        self.assertTrue(full["metadata"]["references"]["assetRN"]["loaded"])
        revision = full["revision"]
        refresh_project(self.store, self.pid, inspect_fn=inspect)
        self.assertEqual(revision, self.store.read()["scenes"][self.sid]["revision"])
        self.assertTrue(self.job())

    def test_full_inspection_failure_blocks_enqueue(self):
        with self.store.transaction() as state:
            state["scenes"][self.sid]["metadata"]["inspection"] = "lightweight"
        def broken(*args, **kwargs):
            raise RuntimeError("missing reference plugin")
        with self.assertRaisesRegex(ValueError, "missing reference plugin"):
            ensure_detailed(self.store, [self.sid], inspect_fn=broken)
        with self.assertRaises(ValueError):
            self.job()
        self.assertEqual({}, self.store.read()["jobs"])

    def test_detailed_inspection_only_loads_selected_scene(self):
        stable_write(self.project / "scenes/b.ma", "second scene")
        calls = []
        def inspect(root, project, scene, version, config=None, payload=None):
            calls.append((Path(scene).name, payload["lightweight"]))
            result = inspector(root, project, scene, version, config)
            result["inspection"] = "lightweight" if payload["lightweight"] else "full"
            return result
        refresh_project(self.store, self.pid, inspect_fn=inspect)
        calls.clear()
        ensure_detailed(self.store, [self.sid], inspect_fn=inspect)
        self.assertEqual([("a.ma", False)], calls)
        other = next(s for s in self.store.read()["scenes"].values() if s["id"] != self.sid)
        self.assertEqual("lightweight", other["metadata"]["inspection"])

    def test_date_scan_reads_no_asset_or_scene_content(self):
        stable_write(self.project / "scenes/b.ma", "second scene")
        texture = self.project / "texture.bin"
        stable_write(texture, "shared texture")
        with patch("farm.assets.sha256", side_effect=AssertionError("No content reads in date scan")) as hashed:
            refresh_project(self.store, self.pid, inspect_fn=inspector)
        hashed.assert_not_called()

    def test_reregister_preserves_project_scan_and_configuration(self):
        with self.store.transaction() as state:
            state["projects"][self.pid].update(extras=[str(self.base / "extra")],
                portable={"creation_root": str(self.base)}, error="retained diagnostic")
        before = self.store.read()["projects"][self.pid]
        self.assertEqual(self.pid, self.store.add_project(self.project, "2027"))
        self.assertEqual(before, self.store.read()["projects"][self.pid])

    def test_reregister_moved_portable_project_preserves_id(self):
        project = self.store.root / "projects" / "portable"
        project.mkdir(parents=True)
        with self.store.transaction() as state:
            state["projects"]["original-portable-id"] = {"id": "original-portable-id",
                "path": "projects/portable", "maya_version": "2027", "name": "Portable", "extras": []}
        self.assertEqual("original-portable-id", self.store.add_project(project, "2027"))
        self.assertEqual(2, len(self.store.read()["projects"]))

    def claim(self):
        return self.store.claim("worker", {"2027": {"plugins": {"mtoa": "test"}}})

    def test_duplicate_enqueue_is_idempotent(self):
        self.assertEqual(self.job(), self.job())
        self.assertEqual(len(self.store.read()["jobs"]), 1)

    def test_maya_version_and_plugin_presence_not_patch_version(self):
        enqueue(self.store, self.sid, maya_version="2027")
        self.assertIsNone(self.store.claim("wrong", {"2026": {"plugins": {"mtoa": "test"}}}))
        self.assertIsNone(self.store.claim("wrong", {"2027": {"plugins": {}}}))
        self.assertIsNotNone(self.store.claim("other-patch", {"2027": {"plugins": {"mtoa": "other"}}}))

    def test_default_job_accepts_other_maya_version_and_keeps_it_on_resume(self):
        jid = self.job()
        job = self.store.read()["jobs"][jid]
        self.assertFalse(job["maya_version_required"])
        self.assertEqual(job["scene_maya_version"], "2027")
        caps = {"2026": {"plugins": {"mtoa": "different"}}}
        claimed = self.store.claim("other-pc", caps)
        self.assertEqual(claimed["maya_version"], "2026")
        self.store.finish(jid, claimed["token"], "paused")
        self.store.action(jid, "resume", self.store.read()["jobs"][jid]["revision"])
        self.assertIsNone(self.store.claim("wrong-version", {"2027": {"plugins": {"mtoa": "test"}}}))
        self.assertEqual(self.store.claim("same-version", caps)["maya_version"], "2026")

    def test_pinned_and_unpinned_jobs_have_distinct_signatures(self):
        flexible = self.job()
        pinned = enqueue(self.store, self.sid, maya_version="2027")
        self.assertNotEqual(flexible, pinned)
        self.assertEqual(pinned, enqueue(self.store, self.sid, maya_version="2027"))
        self.assertTrue(self.store.read()["jobs"][pinned]["maya_version_required"])

    def test_unpinned_job_probes_required_plugins_on_every_available_maya(self):
        with self.store.transaction() as state:
            state["scenes"][self.sid]["metadata"]["plugins"] = {"mtoa": "5.6.2.1", "custom": "1.0"}
        self.job()
        inspected = []
        def inspect_capabilities(root, project, scene, version, config, mode, payload):
            inspected.append((version, set(payload["plugins"])))
            return {"plugins": {name: "different" for name in payload["plugins"]}, "oiiotool": "available"}
        with patch("farm.worker.installations", return_value={"2026": "Maya2026", "2027": "Maya2027"}), \
             patch("farm.worker.probe", side_effect=inspect_capabilities), \
             patch("farm.post.check_dependencies", return_value={}):
            caps, errors = diagnose(self.store.root, {"enabled_versions": ["2026", "2027"]})
        self.assertEqual(errors, {})
        self.assertEqual(set(caps), {"2026", "2027"})
        self.assertEqual(inspected, [("2026", {"mtoa", "custom"}), ("2027", {"mtoa", "custom"})])

    def test_additional_plugin_must_be_present_but_patch_may_differ(self):
        with self.store.transaction() as state:
            state['scenes'][self.sid]['metadata']['plugins'] = {'mtoa': '5.6.2.1', 'custom': '1.2.3'}
        jid = self.job()
        job = self.store.read()['jobs'][jid]
        self.assertEqual(job['plugin_versions']['mtoa'], '5.6.2.1')
        self.assertNotIn('mtoa', job['plugins'])
        self.assertIsNone(self.store.claim('missing', {'2027': {'plugins': {'mtoa': '5.6.2'}}}))
        claimed = self.store.claim('compatible', {'2027': {'plugins': {'mtoa': '5.6.2', 'custom': '1.3.0'}}})
        self.assertEqual(claimed['id'], jid)

    def test_gpu_and_post_capabilities(self):
        self.job(device="GPU")
        self.assertIsNone(self.claim())

    def test_atomic_claim_across_processes(self):
        jid = self.job()
        ctx = multiprocessing.get_context("spawn")
        gate, queue = ctx.Event(), ctx.Queue()
        children = [ctx.Process(target=concurrent_claim, args=(str(self.store.root), gate, queue, index)) for index in range(6)]
        for child in children:
            child.start()
        gate.set()
        answers = [queue.get(timeout=30) for _ in children]
        for child in children:
            child.join(timeout=10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(answers.count(jid), 1)

    def test_stale_token_cannot_heartbeat_or_finish(self):
        jid = self.job()
        self.claim()
        with self.assertRaises(PermissionError):
            self.store.heartbeat(jid, "stale", completed=1)
        with self.assertRaises(PermissionError):
            self.store.finish(jid, "stale", "complete")

    def test_stale_ui_revision_rejected(self):
        jid = self.job()
        self.store.action(jid, "hold", 0)
        with self.assertRaises(ValueError):
            self.store.action(jid, "resume", 0)

    def test_delete_running_waits_for_worker(self):
        jid = self.job()
        claimed = self.claim()
        self.store.action(jid, "delete", claimed["revision"])
        self.assertEqual(self.store.read()["jobs"][jid]["state"], "claimed")
        self.store.finish(jid, claimed["token"], "paused")
        self.assertEqual(self.store.read()["jobs"][jid]["state"], "deleted")

    def test_project_update_holds_pending_without_autoenqueue(self):
        jid = self.job()
        stable_write(self.project / "scenes" / "a.ma", "changed scene")
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        state = self.store.read()
        self.assertEqual(state["scenes"][self.sid]["state"], "updated")
        self.assertEqual(state["jobs"][jid]["state"], "held")
        self.assertEqual(len(state["jobs"]), 1)
        with self.assertRaises(ValueError):
            self.store.action(jid, "resume", state["jobs"][jid]["revision"])

    def test_running_old_revision_does_not_complete_new_revision(self):
        jid = self.job()
        claimed = self.claim()
        stable_write(self.project / "scenes" / "a.ma", "new version")
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        self.store.finish(jid, claimed["token"], "complete", publication="old/result")
        self.assertEqual(self.store.read()["scenes"][self.sid]["state"], "updated")

    def test_new_direct_version_holds_old_job_without_autoenqueue(self):
        jid = self.job()
        history = self.project / "scenes/a_v2.ma"
        stable_write(history, "newest version")
        stamp = time.time() - 5
        os.utime(history, (stamp, stamp))
        refresh_project(self.store, self.pid)
        state = self.store.read()
        self.assertEqual("scenes/a_v2.ma", state["scenes"][self.sid]["relative"])
        self.assertEqual("updated", state["scenes"][self.sid]["state"])
        self.assertEqual("held", state["jobs"][jid]["state"])
        self.assertEqual(1, len(state["jobs"]))

    def test_saving_latest_version_is_not_completed_by_old_job(self):
        jid = self.job()
        claimed = self.claim()
        history = self.project / "scenes/a_v2.ma"
        stable_write(history, "saving latest version")
        os.utime(history, None)
        refresh_project(self.store, self.pid)
        self.store.finish(jid, claimed["token"], "complete", publication="old/result")
        scene = self.store.read()["scenes"][self.sid]
        self.assertEqual("error", scene["state"])
        self.assertTrue(scene["saving"])
        self.assertNotEqual(claimed["scene_revision"], scene["revision"])

    def test_dependency_update_changes_revision(self):
        stable_write(self.project / "texture.bin", "texture one")
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        ensure_detailed(self.store, [self.sid], inspect_fn=inspector)
        revision = self.store.read()["scenes"][self.sid]["revision"]
        stable_write(self.project / "texture.bin", "texture two")
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        self.assertEqual(revision, self.store.read()["scenes"][self.sid]["revision"])
        ensure_detailed(self.store, [self.sid], inspect_fn=inspector)
        self.assertNotEqual(revision, self.store.read()["scenes"][self.sid]["revision"])

    def test_extra_folder_update_invalidates_scene(self):
        extra = self.base / "custom_plugin_assets"
        stable_write(extra / "cache.dat", "one")
        with self.store.transaction() as state:
            state["projects"][self.pid]["extras"] = [str(extra)]
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        ensure_detailed(self.store, [self.sid], inspect_fn=inspector)
        revision = self.store.read()["scenes"][self.sid]["revision"]
        stable_write(extra / "cache.dat", "two")
        jid = self.job()
        self.assertIsNone(self.store.read()["jobs"][jid]["snapshot"])
        self.assertEqual("direct", self.store.read()["jobs"][jid]["source_mode"])
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        ensure_detailed(self.store, [self.sid], inspect_fn=inspector)
        self.assertNotEqual(revision, self.store.read()["scenes"][self.sid]["revision"])

    def test_unchanged_scan_does_not_reinspect(self):
        calls = []
        def count(*args, **kwargs):
            calls.append(1)
            return inspector(*args, **kwargs)
        refresh_project(self.store, self.pid, inspect_fn=count)
        self.assertEqual(calls, [])

    def test_deleted_scene_blocks_new_jobs(self):
        (self.project / "scenes/a.ma").unlink()
        refresh_project(self.store, self.pid, inspect_fn=inspector)
        self.assertEqual(self.store.read()["scenes"][self.sid]["state"], "missing")
        with self.assertRaises(ValueError):
            self.job()

    def test_snapshot_immune_to_project_edits(self):
        jid = self.job()
        snapshot = read_json(self.store.root / create_snapshot(self.store, self.store.read()["scenes"][self.sid]))
        stable_write(self.project / "scenes/a.ma", "new source")
        materialize(self.store.root, snapshot, self.store.root / "workers/test/sources")
        self.assertEqual((self.store.root / "workers/test/sources/project/scenes/a.ma").read_text(), "test scene")

    def test_materialization_preserves_direct_selection_mtime(self):
        history = self.project / "scenes/a_v2.ma"
        stable_write(history, "newest direct version")
        stamp = time.time() - 5
        os.utime(history, (stamp, stamp))
        refresh_project(self.store, self.pid)
        ensure_detailed(self.store, [self.sid], inspect_fn=inspector)
        jid = self.job()
        snapshot = read_json(self.store.root / create_snapshot(self.store, self.store.read()["scenes"][self.sid]))
        destination = self.store.root / "workers/portable/sources"
        materialize(self.store.root, snapshot, destination)
        candidate = next(iter(latest_scenes(destination / "project").values()))
        self.assertEqual("scenes/a_v2.ma", candidate["relative"])
        self.assertEqual(history.stat().st_mtime_ns, (destination / "project" / candidate["relative"]).stat().st_mtime_ns)

    def test_enqueue_uses_saved_information_without_update_check(self):
        stable_write(self.project / "scenes/a.ma", "unscanned edit")
        with patch("farm.assets.latest_scenes", side_effect=AssertionError("Must not check source dates")), \
             patch("farm.assets.inventory", side_effect=AssertionError("Must not enumerate assets")), \
             patch("farm.assets.create_snapshot", side_effect=AssertionError("Must not copy project")), \
             patch("farm.assets.ensure_detailed", side_effect=AssertionError("Must not inspect scene")):
            jid = enqueue(self.store, self.sid)
        self.assertEqual("pending", self.store.read()["jobs"][jid]["state"])

    def test_paths_cannot_escape_root(self):
        with self.assertRaises(ValueError):
            inside(self.store.root, "../escape")

    def test_transient_smb_read_denial_is_retried(self):
        with patch.object(Path, "read_text", side_effect=[PermissionError("sharing violation"), '{"ok":true}']):
            self.assertEqual(read_json(self.store.root / "test.json"), {"ok": True})

    def test_byte_lock_cannot_be_stolen(self):
        path = self.store.root / "test.lock"
        with FileLock(path):
            with self.assertRaises(TimeoutError):
                with FileLock(path, timeout=0):
                    pass

    def test_watchdog_does_not_reset_for_logs_or_incomplete_files(self):
        now = [0]
        watch = Watchdog(1800, clock=lambda: now[0])
        status = {"state": "rendering", "unit": "f1", "attempt": "1", "log_updated": 0}
        self.assertFalse(watch.expired(status))
        now[0] = 1799
        status["log_updated"] = 1799
        self.assertFalse(watch.expired(status))
        now[0] = 1800
        self.assertTrue(watch.expired(status))
        status["unit"] = "f2"
        self.assertFalse(watch.expired(status))

    def test_watchdog_excludes_postprocessing_and_opening(self):
        now = [0]
        watch = Watchdog(1, clock=lambda: now[0])
        for phase in ["opening", "postprocessing", "publishing", "waiting_io"]:
            self.assertFalse(watch.expired({"state": phase}))
            now[0] += 1000
            self.assertFalse(watch.expired({"state": phase}))

    def test_unicode_mayapy_bootstrap(self):
        from farm.common import python_command, runtime_env
        script = self.base / "日本語 script.py"
        script.write_text("import sys; print(sys.argv[1])", encoding="utf-8")
        result = subprocess.run(python_command(sys.executable, script, ["日本語の引数"]),
                                capture_output=True, encoding="utf-8", env=runtime_env(self.store.root), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "日本語の引数")

    def test_portable_identity_survives_drive_change(self):
        from farm.assets import portable_identity
        original = self.base / "USB_original"
        moved = self.base / "USB_moved"
        project = {"portable": {"creation_root": str(original), "mappings": [["D:/original_assets", "projects/p/external"]]}}
        old = {str(original / "projects/p/external/texture.png"): [str(original / "projects/p/external/texture.png"), "hash"]}
        new = {str(moved / "projects/p/external/texture.png"): [str(moved / "projects/p/external/texture.png"), "hash"]}
        self.assertEqual(portable_identity(old, project, moved), portable_identity(new, project, moved))

    @unittest.skipUnless(os.name == "nt", "Windows DOS alias")
    def test_dos_alias_keeps_data_inside_farm(self):
        from farm.common import DosAlias
        target = self.store.root / "日本語"
        target.mkdir()
        with DosAlias(target, self.store.root / "alias.json") as alias:
            (Path(alias) / "marker.txt").write_text("same data")
            self.assertEqual((target / "marker.txt").read_text(), "same data")
        self.assertFalse((self.store.root / "alias.json").exists())

    def test_validation_rejects_invalid_settings(self):
        for override in [{"end": -1}, {"step": 0}, {"timeout_minutes": 0}, {"cameras": []}, {"format": "jpeg"},
                         {"format": "exr", "post": {"video": True}}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_settings(override, metadata())

    def test_publish_rejects_stale_owner_and_retains_staging(self):
        jid = self.job(end=1)
        job = self.claim()
        work = self.store.root / "workers/test/jobs" / jid
        image = work / "raw/f1/beauty.png"
        stable_write(image, "test payload")
        key = digest(("layer", "camera", 1))[:24]
        atomic_json(work / "frames.json", {key: {"layer": "layer", "camera": "camera", "frame": 1,
            "signature": job["signature"], "files": [{"path": "raw/f1/beauty.png", "sha256": sha256(image)}]}})
        job["token"] = "expired"
        with self.assertRaises(PermissionError):
            publication(self.store, job, work)
        self.assertTrue(list((self.store.root / "staging").iterdir()))
        self.assertNotEqual(self.store.read()["jobs"][jid]["state"], "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)

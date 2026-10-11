import unittest
from unittest.mock import patch
import test_core as fixtures
from farm.package import export_package
from farm.common import read_json, sha256
from farm.assets import ensure_detailed, resolve_project_asset
from farm.maya_entry import mapped_path


class PackageProgressTests(unittest.TestCase):
    def test_package_preparation_inspects_only_selected_scene_without_inventory(self):
        fixture = fixtures.CoreTests(methodName="runTest")
        fixture.setUp()
        fixtures.stable_write(fixture.project / "scenes/unselected.ma", "not selected")
        calls, events = [], []
        def inspect(root, project, scene, version, config, **options):
            calls.append(scene)
            return {**fixtures.metadata(), "inspection": "full"}
        with patch("farm.assets.inventory", side_effect=AssertionError("No project hash before inspection")), \
             patch("farm.assets.latest_scenes", side_effect=AssertionError("No full scene list before inspection")), \
             patch("farm.assets.refresh_project", side_effect=AssertionError("No project refresh before inspection")):
            ensure_detailed(fixture.store, [fixture.sid], inspect_fn=inspect, progress=events.append, selected_only=True)
        self.assertEqual(calls, [fixture.project / "scenes/a.ma"])
        self.assertTrue(events[0]["phase"].startswith("選択シーンをMayaで詳細調査中"))

    def test_selected_inspection_failure_blocks_old_metadata_enqueue(self):
        fixture = fixtures.CoreTests(methodName="runTest")
        fixture.setUp()
        def inspect(*args, **kwargs):
            return {**fixtures.metadata(), "missing": ["E:/missing/rig.mb"]}
        with self.assertRaisesRegex(ValueError, "E:/missing/rig.mb"):
            ensure_detailed(fixture.store, [fixture.sid], inspect_fn=inspect, selected_only=True)
        self.assertEqual(fixture.store.read()["scenes"][fixture.sid]["state"], "error")
        with self.assertRaises(ValueError):
            fixtures.enqueue(fixture.store, fixture.sid)

    def test_reports_are_emitted_before_preparation_failure(self):
        fixture = fixtures.CoreTests(methodName="runTest")
        fixture.setUp()
        warnings = [{"scene": "shot", "message": "旧パスを読み替え"}]
        mappings = [{"scene": "shot", "source": "E:/old/project", "resolved": str(fixture.project), "target": ""}]
        events = []
        def fail(store, ids, config, progress, **options):
            progress({"phase": "素材の読み替え・警告を確認中", "warnings": warnings, "mappings": mappings})
            raise ValueError("missing asset")
        with patch("farm.package.ensure_detailed", side_effect=fail), self.assertRaisesRegex(ValueError, "missing asset"):
            export_package(fixture.store, fixture.base / "USB failed", [fixture.sid], progress=events.append)
        self.assertEqual(events[-1]["warnings"], warnings)
        self.assertEqual(events[-1]["mappings"], mappings)

    def test_old_root_is_warned_copied_and_resolved_after_usb_move(self):
        fixture = fixtures.CoreTests(methodName="runTest")
        fixture.setUp()
        old_root = "E:/OldPC/" + fixture.project.name
        old_asset = old_root + "/assets/rig.mb"
        fixtures.stable_write(fixture.project / "assets/rig.mb", "reference fixture")
        fixtures.stable_write(fixture.project / "scenes/a.ma", 'file -r "' + old_asset + '";')
        def inspect(*args, **kwargs):
            mappings = kwargs["payload"]["mappings"]
            self.assertIn([old_root, str(fixture.project)], mappings)
            return {**fixtures.metadata(), "inspection": "full", "dependencies": [old_asset],
                    "path_mappings": mappings, "warnings": ["旧パスを読み替え: " + old_root]}
        destination = fixture.base / "USB old paths"
        events = []
        with patch("farm.package.ensure_detailed", side_effect=lambda store, ids, config, progress, **options:
                   ensure_detailed(store, ids, config, inspect_fn=inspect, progress=progress, **options)):
            result = export_package(fixture.store, destination, [fixture.sid], progress=events.append)
        self.assertTrue(result["ready"])
        self.assertTrue(result["warnings"])
        self.assertEqual(read_json(destination / "package_warnings.json"), result["warnings"])
        self.assertEqual(read_json(destination / "package_mappings.json"), result["mappings"])
        self.assertTrue(any(m["source"] == old_root and m["target"].endswith("/project") for m in result["mappings"]))
        report_index = next(i for i, event in enumerate(events) if event.get("mappings"))
        copy_index = next(i for i, event in enumerate(events) if event.get("phase") == "ツールをコピー中")
        self.assertLess(report_index, copy_index)
        packaged = read_json(destination / "data/state.json")
        project = next(iter(packaged["projects"].values()))
        portable = project["portable"]
        self.assertTrue(any(source == old_root for source, _ in portable["mappings"]))
        moved = fixture.base / "USB moved"
        self.assertTrue(moved.resolve().is_relative_to(fixtures.BASE.resolve()))
        # SMB/antivirus may briefly retain a handle after writing many package files.
        import time
        for attempt in range(20):
            try:
                destination.rename(moved)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.2)
        request = {"project": str(moved / project["path"]), "sources": str(moved / portable["sources"]),
                   "mappings": [[source, str(moved / rel)] for source, rel in portable["mappings"]]}
        target = mapped_path(old_asset, request)
        self.assertEqual(sha256(target), sha256(fixture.project / "assets/rig.mb"))
        from pathlib import Path
        self.assertTrue(Path(target).resolve().is_relative_to(moved.resolve()))

    def test_missing_asset_is_not_guessed_by_basename(self):
        fixture = fixtures.CoreTests(methodName="runTest")
        fixture.setUp()
        fixtures.stable_write(fixture.project / "assets/rig.mb", "reference")
        old = "E:/OldPC/" + fixture.project.name + "/wrong/rig.mb"
        self.assertEqual(resolve_project_asset(old, fixture.project), old)

    def test_export_reports_copy_bytes_and_completion_without_changing_content(self):
        fixture = fixtures.CoreTests(methodName="runTest")
        fixture.setUp()
        events = []
        destination = fixture.base / "USB 梱包"
        with patch("farm.package.ensure_detailed"):
            result = export_package(fixture.store, destination, [fixture.sid], progress=events.append)
        self.assertTrue(result["ready"])
        self.assertEqual(events[-1]["phase"], "梱包完了")
        copies = [e for e in events if e.get("phase") == "保存先へ素材をコピー中"]
        self.assertTrue(copies)
        self.assertEqual(copies[-1]["copied_bytes"], result["source_bytes"])
        self.assertEqual(copies[-1]["completed"], copies[-1]["total"])
        self.assertTrue(any(e.get("phase") == "プロジェクトの展開完了" for e in events))
        original = fixture.project / "scenes/a.ma"
        exported = next((destination / "projects").glob("*/project/scenes/a.ma"))
        self.assertEqual(sha256(original), sha256(exported))
        target_jobs = read_json(destination / "data/state.json")["jobs"]
        self.assertTrue(all(job["state"] == "pending" for job in target_jobs.values()))

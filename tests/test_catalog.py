from pathlib import Path
import os
import sys
import time
import unittest
import uuid
from unittest.mock import patch, MagicMock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.catalog import latest_scenes, scene_key
from farm.assets import refresh_project, refresh_scene_list, project_path_mappings
from farm.store import Store


class CatalogTests(unittest.TestCase):
    def test_direct_open_skips_orphan_reference_nodes(self):
        from farm.maya_entry import open_scene
        cmds = MagicMock()
        cmds.ls.return_value = ["sharedReferenceNode", "Rig:_UNKNOWN_REF_NODE_", "Rig:_UNKNOWN_REF_NODE_1", "validRN"]
        cmds.referenceQuery.side_effect = lambda node, **kw: False if kw.get("isLoaded") else "unused"
        with patch.dict(sys.modules, {"maya": MagicMock(cmds=cmds), "maya.cmds": cmds}), \
             patch("farm.maya_entry.remap_attributes"):
            open_scene({"job": {"id": "test"}, "scene": "test.mb", "project": "project",
                        "mappings": [["E:/old", "project"]]})
        cmds.referenceQuery.assert_called_once_with("validRN", isLoaded=True)

    def setUp(self):
        self.base = Path(__file__).resolve().parent / "runtime" / ("catalog_" + uuid.uuid4().hex)
        self.project = self.base / "project"
        self.project.mkdir(parents=True)
        self.now = time.time() - 30

    def scene(self, relative, age):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
        os.utime(path, (self.now + age, self.now + age))
        return path

    def test_direct_scenes_use_mtime_and_never_scan_history_or_nested_folders(self):
        self.scene("scenes/c020/c020_20990101.mb", 0)
        newest = self.scene("scenes/c020/c020_20200101.mb", 5)
        self.scene("scenes/c020/History/c020.mb", 10)
        self.scene("scenes/c020/nested/c020.mb", 10)
        self.scene("scenes/HISTORY/c022.mb", 10)
        self.scene("scenes/.hidden/c023.mb", 10)
        self.scene("scenes/c021/c021.ma", 2)
        iterdir = Path.iterdir
        allowed = {self.project / "scenes", self.project / "scenes/c020", self.project / "scenes/c021"}
        def guarded_iterdir(path):
            self.assertIn(path, allowed, "Must not enumerate History or nested folders")
            return iterdir(path)
        with patch.object(Path, "iterdir", guarded_iterdir):
            selected = latest_scenes(self.project)
        self.assertEqual(2, len(selected))
        cut = selected[scene_key("scenes/c020/c020.mb")]
        self.assertEqual(newest.relative_to(self.project).as_posix(), cut["relative"])
        self.assertEqual(2, cut["candidates"])

    def test_history_only_scene_is_not_cataloged(self):
        self.scene("scenes/c020/History/c020.mb", 5)
        self.assertEqual({}, latest_scenes(self.project))

    def test_project_without_scenes_folder_uses_direct_scene_files(self):
        self.scene("shot.ma", 0)
        self.scene("c020/c020.mb", 0)
        self.scene("c020/History/c020.mb", 5)
        self.assertEqual({"shot.ma", "c020/c020.mb"},
                         {scene["relative"] for scene in latest_scenes(self.project).values()})

    def test_saved_project_roots_are_mapped_to_registered_folder(self):
        file = self.scene("scenes/a.mb", 0)
        file.write_bytes(b"\x00X:/LegacyProjects/project//assets/Character/Rig.mb\x00" +
                         b"F:\\old\\project\\textures\\color.png\x00" +
                         b"\\\\oldserver\\share\\project\\assets\\rig.mb\x00" +
                         b"G:/other/project_backup/assets/wrong.mb\x00")
        mappings = project_path_mappings(self.project, file)
        self.assertEqual([["//oldserver/share/project", str(self.project)],
                          ["F:/old/project", str(self.project)],
                          ["X:/LegacyProjects/project", str(self.project)]], mappings)

    def test_manual_list_caches_settings_without_hashing_and_refreshes_new_version(self):
        self.scene("scenes/shot.ma", 0)
        store = Store(self.base / "farm")
        pid = store.add_project(self.project, "2027")
        calls = []
        info = {"ranges": {"playback": {"start": 11, "end": 21, "step": 1}},
                "time_unit": "pal", "resolution": {"width": 1920, "height": 1080},
                "non_default_cameras": [{"name": "camera", "renderable": True}], "inspection": "full"}
        def inspect(*args, **kwargs):
            calls.append(str(args[2]))
            self.assertTrue(kwargs["payload"]["lightweight"])
            self.assertTrue(kwargs["payload"]["catalog_only"])
            return info
        with patch("farm.assets.inventory", side_effect=AssertionError("Must not hash project")), \
             patch("farm.assets.sha256", side_effect=AssertionError("Must not hash materials")):
            refresh_scene_list(store, pid, inspect_fn=inspect)
            first = next(iter(store.read()["scenes"].values()))
            self.assertEqual(info, first["metadata"])
            refresh_scene_list(store, pid, inspect_fn=inspect)
            self.assertEqual(1, len(calls))
            with store.transaction() as state:
                state["scenes"][first["id"]]["state"] = "complete"
            refresh_scene_list(store, pid, inspect_fn=inspect, force=True)
            self.assertEqual(2, len(calls))
            self.assertEqual(first["revision"], store.read()["scenes"][first["id"]]["revision"])
            self.assertEqual("complete", store.read()["scenes"][first["id"]]["state"])
            self.scene("scenes/History/20000101_shot.ma", 10)
            refresh_scene_list(store, pid, inspect_fn=inspect)
            self.assertEqual(2, len(calls))
            self.assertEqual(first["revision"], store.read()["scenes"][first["id"]]["revision"])
            self.scene("scenes/shot_v2.ma", 5)
            refresh_scene_list(store, pid, inspect_fn=inspect)
            updated = store.read()["scenes"][first["id"]]
            self.assertEqual(3, len(calls))
            self.assertNotEqual(first["revision"], updated["revision"])
            self.assertEqual(info, updated["metadata"])
            self.assertEqual("updated", updated["state"])

    def test_failed_list_inspection_retries_and_does_not_keep_old_settings(self):
        self.scene("scenes/shot.ma", 0)
        store = Store(self.base / "farm")
        pid = store.add_project(self.project, "2027")
        def fail(*args, **kwargs):
            raise RuntimeError("Inspection failed")
        refresh_scene_list(store, pid, inspect_fn=fail)
        sc = next(iter(store.read()["scenes"].values()))
        self.assertEqual("error", sc["state"])
        self.assertEqual({}, sc["metadata"])
        calls = []
        def retry(*args, **kwargs):
            calls.append(True)
            return {"ranges": {}, "time_unit": "film", "resolution": {}, "non_default_cameras": []}
        refresh_scene_list(store, pid, inspect_fn=retry)
        self.assertEqual(1, len(calls))
        self.assertEqual("updated", store.read()["scenes"][sc["id"]]["state"])

    def test_generic_dates_and_history_share_logical_scene(self):
        self.assertEqual(scene_key("scenes/c029/c029.mb"), scene_key("scenes/c029/Histroy_c029/c029_20260903.mb"))
        self.assertEqual(scene_key("scenes/shot.ma"), scene_key("scenes/History/20260927_shot.mb"))
        self.assertEqual(scene_key("scenes/shot.ma"), scene_key("scenes/History/shot_20260927.mb"))
        self.assertNotEqual(scene_key("scenes/roomA/shot.ma"), scene_key("scenes/roomB/shot.ma"))

    def test_new_direct_version_keeps_id_and_marks_update(self):
        self.scene("scenes/c020/c020.mb", 0)
        self.scene("assets/character/History/20990101_character.ma", 10)
        store = Store(self.base / "farm")
        pid = store.add_project(self.project, "2027")
        with patch("farm.assets.inventory", side_effect=AssertionError("Must not read assets")), \
             patch("farm.assets.probe", side_effect=AssertionError("Must not start Maya")):
            refresh_project(store, pid)
            first = store.read()
            self.assertEqual(1, len(first["scenes"]))
            sid = next(iter(first["scenes"]))
            self.scene("scenes/c020/c020_20200101.mb", 5)
            refresh_project(store, pid)
        after = store.read()
        self.assertEqual([sid], list(after["scenes"]))
        self.assertEqual("updated", after["scenes"][sid]["state"])
        self.assertNotEqual(first["scenes"][sid]["revision"], after["scenes"][sid]["revision"])
        self.assertEqual({}, after["jobs"])

    def test_latest_saving_file_does_not_fall_back_to_history(self):
        self.scene("scenes/c020/History/20200101_c020.mb", 0)
        latest = self.scene("scenes/c020/c020.mb", 1)
        os.utime(latest, None)
        store = Store(self.base / "farm")
        pid = store.add_project(self.project, "2027")
        refresh_project(store, pid)
        sid = next(iter(store.read()["scenes"]))
        self.assertEqual("error", store.read()["scenes"][sid]["state"])
        os.utime(latest, (self.now + 5, self.now + 5))
        refresh_project(store, pid)
        scene = store.read()["scenes"][sid]
        self.assertEqual("scenes/c020/c020.mb", scene["relative"])
        self.assertNotEqual("error", scene["state"])

    def test_equal_mtime_prefers_current_file(self):
        self.scene("scenes/c020/c020.mb", 0)
        self.scene("scenes/c020/History/20200101_c020.mb", 0)
        result = next(iter(latest_scenes(self.project).values()))
        self.assertEqual("scenes/c020/c020.mb", result["relative"])

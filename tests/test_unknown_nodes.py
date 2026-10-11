import unittest
from unittest.mock import MagicMock, patch
import sys
from farm.maya_entry import unknown_scene_details, unknown_scene_warnings
from farm.maya_entry import remap_attributes
from pathlib import Path


class UnknownNodeTests(unittest.TestCase):
    def test_inspection_collects_missing_assets_and_render_warns(self):
        cmds = MagicMock()
        cmds.file.return_value = []
        project = Path(__file__).parent / "runtime" / "unknown_missing_fixture"
        missing = str(project / "missing.png")
        request = {"project": str(project), "mode": "inspect"}
        with patch.dict(sys.modules, {"maya": MagicMock(cmds=cmds), "maya.cmds": cmds}), \
             patch("farm.maya_entry.file_attributes", return_value={"one.fileTextureName": missing, "two.fileTextureName": missing}), \
             patch("farm.assets.expand_asset", return_value=[]):
            remap_attributes(request)
            self.assertEqual(len(request["_missing_assets"]), 1)
            request["mode"] = "render"
            remap_attributes(request)
            self.assertEqual(len(request["_asset_warnings"]), 1)
            self.assertIn("missing.png", request["_asset_warnings"][0])
            cmds.setAttr.assert_any_call("defaultArnoldRenderOptions.abortOnError", False)
            request.pop("mode")
            request["job"] = {"id": "resume"}
            remap_attributes(request)
            self.assertEqual(len(request["_missing_assets"]), 1)

    def test_binary_editor_tags_are_warnings_without_name_guessing(self):
        cmds = MagicMock()
        cmds.ls.side_effect = lambda **kw: ["renamedNode"] if kw["type"] == "unknown" else []
        cmds.unknownNode.side_effect = lambda node, **kw: 0x4e474549 if kw.get("realClassTag") else ""
        cmds.unknownPlugin.return_value = []
        with patch.dict(sys.modules, {"maya": MagicMock(cmds=cmds), "maya.cmds": cmds}):
            details = unknown_scene_details()
        self.assertTrue(details["nodes"][0]["editor_only"])
        self.assertIn("renamedNode", unknown_scene_warnings(details)[0])

    def test_editor_looking_name_with_unknown_render_type_still_fails(self):
        details = {"nodes": [{"node": "hyperShadePrimaryNodeEditorSavedTabsInfo", "realClassName": "customShader",
                              "realClassTag": 123, "plugin": "CustomRenderer", "editor_only": False}], "plugins": []}
        with self.assertRaisesRegex(RuntimeError, "CustomRenderer"):
            unknown_scene_warnings(details)

    def test_custom_plugin_data_remains_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "RigData"):
            unknown_scene_warnings({"nodes": [], "plugins": [{"plugin": "RigPlugin", "node_types": [],
                "data_types": ["RigData"], "editor_only": False}]})

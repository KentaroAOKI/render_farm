import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch
from farm import maya_entry


class SceneMetadataTests(unittest.TestCase):
    def test_catalog_returns_metadata_despite_unknown_nodes_without_arnold(self):
        maya = ModuleType("maya")
        cmds = MagicMock()
        maya.cmds = cmds
        cmds.ls.return_value = ["SavedEditorNode"]
        cmds.unknownPlugin.return_value = ["MissingLegacyPlugin"]
        cmds.getAttr.side_effect = lambda attr: {"defaultResolution.width": 1920,
            "defaultResolution.height": 1080, "defaultResolution.pixelAspect": 1}[attr]
        cmds.currentUnit.return_value = "film"
        cmds.about.return_value = "2027"
        cmds.objExists.return_value = False
        cmds.pluginInfo.return_value = []
        ranges = {key: {"start": 1, "end": 50, "step": 2} for key in ("playback", "animation", "render")}
        cameras = [{"name": "Shape", "shape": "Shape", "transform": "Camera", "default": False, "renderable": True}]
        with patch.dict(sys.modules, {"maya": maya, "maya.cmds": cmds}), \
             patch.object(maya_entry, "scene_ranges", return_value=ranges), \
             patch.object(maya_entry, "camera_info", return_value=cameras), \
             patch.object(maya_entry, "frame_rate", return_value="24"), \
             patch.object(maya_entry, "configure_mappings"), \
             patch.object(maya_entry, "layers", return_value=[{"name": "defaultRenderLayer", "enabled": True}]), \
             patch.object(maya_entry, "activate_layer"), \
             patch.object(maya_entry, "file_attributes", return_value={}), \
             patch.object(maya_entry, "saved_reference_settings", return_value={}), \
             patch.object(maya_entry, "setup_arnold", side_effect=AssertionError("Catalog must not initialize Arnold")):
            info = maya_entry.inspect_scene({"catalog_only": True, "project": "Project", "scene": "Scene"})
        self.assertEqual(ranges, info["ranges"])
        self.assertEqual(cameras, info["non_default_cameras"])
        self.assertEqual("catalog", info["inspection"])
        self.assertEqual(2, len(info["warnings"]))
        self.assertEqual(["SavedEditorNode"], info["unknown_nodes"])
        cmds.file.assert_any_call("Scene", open=True, force=True, prompt=False,
                                          loadReferenceDepth="none", executeScriptNodes=False)

    def test_full_inspection_still_rejects_unknown_nodes(self):
        maya = ModuleType("maya")
        cmds = MagicMock()
        maya.cmds = cmds
        cmds.ls.return_value = ["UnknownNode"]
        with patch.dict(sys.modules, {"maya": maya, "maya.cmds": cmds}), \
             patch.object(maya_entry, "setup_arnold"), patch.object(maya_entry, "configure_mappings"), \
             patch.object(maya_entry, "open_scene"):
            with self.assertRaisesRegex(RuntimeError, "UnknownNode"):
                maya_entry.inspect_scene({"project": "Project", "scene": "Scene"})

import tempfile
from pathlib import Path
import unittest
from farm.asset_paths import TextureResolver
from farm.assets import project_path_mappings
from farm.common import atomic_json


class AssetPathsTests(unittest.TestCase):
    def test_unique_suffix_ambiguity_and_sequences(self):
        base = Path(__file__).parent / "runtime"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            project = Path(folder)
            for relative in ["Eye/eye.png", "A/shared.png", "B/shared.png", "Tiles/skin.1001.png", "Tiles/skin.1002.png"]:
                p = project / "sourceimages" / relative
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"test")
            resolver = TextureResolver(project)
            self.assertEqual(resolver.resolve("E:/old/eye.png"), str(project.resolve() / "sourceimages/Eye/eye.png"))
            self.assertIsNone(resolver.resolve("E:/old/shared.png"))
            self.assertEqual(resolver.resolve("E:/old/B/shared.png"), str(project.resolve() / "sourceimages/B/shared.png"))
            self.assertEqual(resolver.resolve("E:/old/skin.<UDIM>.png"), str(project.resolve() / "sourceimages/Tiles/skin.<UDIM>.png"))
            self.assertIsNone(resolver.resolve("E:/old/missing.png"))
            scene = project / "scene.ma"
            scene.write_text("// fixture", encoding="utf-8")
            atomic_json(project / "asset_paths.json", {"mappings": [["E:/old", "sourceimages/Eye"]]})
            self.assertEqual(project_path_mappings(project, scene), [["E:/old", str(project / "sourceimages/Eye")]])

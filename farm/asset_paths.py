"""Relocate textures without editing scene files or choosing ambiguous matches."""
from pathlib import Path
import re


class TextureResolver:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.files = None

    def resolve(self, value):
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        if not name:
            return None
        if self.files is None:
            self.files = [p for p in (self.project / "sourceimages").rglob("*") if p.is_file()]
        pattern = re.escape(name)
        pattern = re.sub(r"<UDIM>|<f>", r"\\d+", pattern, flags=re.I)
        pattern = re.sub(r"(?:\\#)+", lambda m: r"\d{" + str(len(m.group()) // 2) + "}", pattern)
        candidates = [p for p in self.files if re.fullmatch(pattern, p.name, re.I)]
        parts = value.replace("\\", "/").casefold().split("/")[:-1]
        def score(path):
            count = 0
            for a, b in zip(reversed(parts), reversed(path.parent.as_posix().casefold().split("/"))):
                if a != b:
                    break
                count += 1
            return count
        if not candidates:
            return None
        best = max(map(score, candidates))
        parents = {p.parent for p in candidates if score(p) == best}
        if len(parents) != 1:
            return None
        # Keep sequence/UDIM tokens. For plain filenames preserve actual filesystem case.
        if not re.search(r"<UDIM>|<f>|#", name, re.I):
            return str(next(p for p in candidates if p.parent in parents))
        return str(next(iter(parents)) / name)

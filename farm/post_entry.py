"""Optional denoising and verified FFV1 encoding, executed outside Maya."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import re
from fractions import Fraction

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farm.common import atomic_json, command_flags, sha256


def png_rgb(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size, im.convert("RGB").tobytes()
    except ImportError:
        from PySide6.QtGui import QImage
        im = QImage(str(path)).convertToFormat(QImage.Format_RGB888)
        if im.isNull():
            raise ValueError("PNGを読み込めません: " + str(path))
        data = bytes(im.constBits())
        stride, width, height = im.bytesPerLine(), im.width(), im.height()
        return (width, height), b"".join(data[y * stride:y * stride + width * 3] for y in range(height))


def main():
    req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = Path(req["output"])
    images = [Path(p) for p in req["input"]]
    def progress(phase, completed=0, **details):
        atomic_json(output / "progress.json", {"phase": phase, "completed": completed, "total": len(images), **details})
    progress("入力画像を確認中")
    hashes = [sha256(p) for p in images]
    if req["options"].get("fastdvdnet"):
        import numpy as np
        import torch
        from PIL import Image
        from functools import lru_cache
        config = req["config"]
        sys.path.insert(0, config["fastdvdnet_repo"])
        from models import FastDVDnet
        from fastdvdnet import temp_denoise
        device = "cuda" if torch.cuda.is_available() else "cpu"
        progress("モデルを読み込み中", device=device)
        model = FastDVDnet()
        weights = torch.load(config["fastdvdnet_model"], map_location="cpu", weights_only=True)
        model.load_state_dict({key.removeprefix("module."): value for key, value in weights.items()})
        model.eval().to(device)
        @lru_cache(maxsize=7)
        def tensor(index):
            with Image.open(images[index]) as im:
                value = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
            return torch.from_numpy(value.transpose(2, 0, 1).copy())
        def reflected(index):
            if len(images) == 1:
                return 0
            period = 2 * (len(images) - 1)
            mod = index % period
            return min(mod, period - mod)
        height, width = tensor(0).shape[1:]
        noise = torch.full((1, 1, height, width), 10 / 255.0, device=device)
        denoised = []
        with torch.inference_mode():
            for index in range(len(images)):
                stack = torch.cat([tensor(reflected(i)) for i in range(index - 2, index + 3)])[None].to(device)
                clean = temp_denoise(model, stack, noise)[0]
                if not torch.isfinite(clean).all():
                    raise ValueError("デノイズ結果に非有限値があります")
                pixels = clean.mul(255).round().clamp(0, 255).byte().permute(1, 2, 0).cpu().numpy()
                path = output / ("frame.%06d.png" % index)
                Image.fromarray(pixels).save(path)
                denoised.append(path)
                progress("デノイズ", index + 1, device=device)
        tensor.cache_clear()
        images = denoised
    report = {"verified": True, "frames": len(images), "fastdvdnet_sigma": 10 if req["options"].get("fastdvdnet") else None}
    if req["options"].get("video"):
        ffmpeg = req["config"]["ffmpeg"]
        size, _ = png_rgb(images[0])
        video = output / "video.mkv"
        rate = Fraction(str(req["fps"]))
        expected = []
        with (output / "ffmpeg.log").open("wb") as log:
            process = subprocess.Popen([ffmpeg, "-v", "error", "-n", "-f", "rawvideo", "-pixel_format", "rgb24",
                "-video_size", "%dx%d" % size, "-framerate", str(rate), "-i", "pipe:0", "-an", "-c:v", "ffv1",
                "-level", "3", "-pix_fmt", "bgr0", video.name], cwd=output, stdin=subprocess.PIPE, stdout=log, stderr=log, **command_flags())
            try:
                for index, image in enumerate(images):
                    actual, pixels = png_rgb(image)
                    if actual != size:
                        raise ValueError("連番画像の寸法が一致しません")
                    expected.append(hashlib.md5(pixels).hexdigest())
                    process.stdin.write(pixels)
                    progress("動画化", index + 1)
                process.stdin.close()
                if process.wait() != 0:
                    raise RuntimeError("FFV1エンコードに失敗しました")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        progress("動画を検証中", len(images))
        verification = subprocess.run([ffmpeg, "-v", "error", "-i", video.name, "-map", "0:v:0", "-pix_fmt", "rgb24",
                                       "-f", "framemd5", "-"], cwd=output, capture_output=True, text=True, check=True, **command_flags()).stdout
        actual = [line.rsplit(",", 1)[-1].strip() for line in verification.splitlines() if line and not line.startswith("#")]
        if actual != expected:
            raise ValueError("動画の復号フレーム数またはRGBハッシュが一致しません")
        timebase = re.search(r"#tb 0:\s*(\S+)", verification)
        if not timebase or Fraction(timebase.group(1)) != 1 / rate:
            raise ValueError("動画のフレームレートが一致しません")
        (output / "verification.framemd5").write_text(verification, encoding="utf-8")
        report.update(lossless_rgb_hash_match=True, decoded_frames=len(actual), bytes=video.stat().st_size, fps=str(rate))
    original = [Path(p) for p in req["input"]]
    if [sha256(p) for p in original] != hashes:
        raise ValueError("後処理中に入力が変更されました")
    atomic_json(req["result"], report)
    progress("完了", len(images))


if __name__ == "__main__":
    main()

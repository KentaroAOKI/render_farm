"""Run with the farm's WinPython: install only inference dependencies under this root."""
from pathlib import Path
import json
import os
import subprocess
import sys
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import atomic_json, runtime_env, sha256, worker_dir

work = worker_dir(ROOT) / "setup_fastdvdnet"
work.mkdir(parents=True, exist_ok=True)
env = runtime_env(ROOT, work)
env.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
os.environ.update(env)

def run(arguments):
    subprocess.run([sys.executable, "-B", "-m", "pip", "--isolated", "install", "--no-cache-dir", *arguments],
                   env=env, cwd=ROOT, check=True)

print("Installing NumPy / Pillow", flush=True)
run(["numpy>=1.26,<3", "pillow>=10,<13"])
print("Installing PyTorch 2.7.1 CUDA 12.6", flush=True)
run(["torch==2.7.1", "--index-url", "https://download.pytorch.org/whl/cu126"])

repo = ROOT / "tools/fastdvdnet"
repo.mkdir(parents=True, exist_ok=True)
def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Portable-Maya-Farm-Setup"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()

print("Downloading official FastDVDnet code and pretrained model", flush=True)
commit = json.loads(fetch("https://api.github.com/repos/m-tassano/fastdvdnet/commits/master"))["sha"]
files = {}
for name in ("models.py", "fastdvdnet.py", "model.pth", "LICENSE", "README.md"):
    path = repo / name
    content = fetch("https://raw.githubusercontent.com/m-tassano/fastdvdnet/" + commit + "/" + name)
    if name == "model.pth" and content.startswith(b"version https://git-lfs"):
        raise RuntimeError("Model download returned a Git LFS pointer")
    if path.exists() and path.read_bytes() != content:
        raise RuntimeError("Existing FastDVDnet file differs; refusing overwrite: " + str(path))
    path.write_bytes(content)
    files[name] = {"bytes": len(content), "sha256": sha256(path)}
    print(name, len(content), "bytes", flush=True)
atomic_json(repo / "installation.json", {"source": "https://github.com/m-tassano/fastdvdnet", "commit": commit, "files": files})
freeze = subprocess.check_output([sys.executable, "-B", "-m", "pip", "freeze"], env=env, text=True)
(work / "requirements-installed.txt").write_text(freeze, encoding="utf-8")
print("Dependencies and model installed. Configuration and inference verification are next.", flush=True)

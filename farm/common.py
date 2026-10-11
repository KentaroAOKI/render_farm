from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import time
import uuid
import base64

SUPPORTED = ("2025", "2026", "2027")


def read_json(path, default=None):
    for attempt in range(12):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except PermissionError:
            # SMB/antivirus may briefly deny a new reader during atomic replacement.
            if attempt == 11:
                raise
            time.sleep(min(.025 * (attempt + 1), .25))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A full UUID can push UNC paths beyond the Win32 260-character limit.
    # Sixteen hex digits still give each write a 64-bit random suffix.
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex[:16] + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.1)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def sha256(path, progress=None):
    h = hashlib.sha256()
    read = 0
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
            read += len(block)
            if progress:
                progress(read)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inside(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("ルート外の作業パスを拒否しました: " + str(relative))
    return target


def safe_name(text):
    label = re.sub(r'[^\w.-]+', '_', text, flags=re.UNICODE).strip(". ")[:60] or "item"
    if label.upper() in {"CON", "PRN", "AUX", "NUL", *("COM%d" % n for n in range(1, 10)), *("LPT%d" % n for n in range(1, 10))}:
        label = "_" + label
    return label


class FileLock:
    """OS byte-range lock (SMB supported); never unlink/recreate the lock file."""
    def __init__(self, path, timeout=15):
        self.path, self.timeout, self.handle = Path(path), timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError("他の処理が使用中です: " + str(self.path))
                time.sleep(.1)

    def __exit__(self, *_):
        if self.handle:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def machine_id():
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            identity = winreg.QueryValueEx(key, "MachineGuid")[0]
    else:
        identity = str(uuid.getnode())
    hostname = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname()) or "PC"
    return safe_name(hostname) + "_" + digest(identity)[:12]


def worker_dir(root):
    path = inside(root, "workers/" + machine_id())
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_env(root, work=None):
    root = Path(root).resolve()
    work = Path(work or worker_dir(root)).resolve()
    if not work.is_relative_to(root):
        raise ValueError("作業領域はツールルート内に必要です")
    env = os.environ.copy()
    for name, folder in {"TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp",
                         "MAYA_APP_DIR": "maya", "TORCH_HOME": "cache/torch",
                         "XDG_CACHE_HOME": "cache", "CUDA_CACHE_PATH": "cache/cuda",
                         "PYTHONPYCACHEPREFIX": "cache/pycache"}.items():
        target = work / folder
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               MAYA_DISABLE_CIP="1", MAYA_DISABLE_CER="1", MAYA_SKIP_USERSETUP_PY="1",
               MAYA_RENDER_SETUP_INCLUDE_ALL_LIGHTS="1")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def installations(overrides=None):
    result = {}
    for version in SUPPORTED:
        base = Path((overrides or {}).get(version) or (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Autodesk" / ("Maya" + version)))
        if (base / "bin/mayapy.exe").is_file() and (base / "bin/maya.exe").is_file():
            result[version] = str(base.resolve())
    return result


def command_flags():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def maya_env(root, work, installation):
    """Do not let the dashboard's mayapy version leak into a different Maya child."""
    env = runtime_env(root, work)
    for key in ("PYTHONHOME", "MAYA_LOCATION", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    for key in ("PATH", "PYTHONPATH", "MAYA_SCRIPT_PATH", "MAYA_PLUG_IN_PATH", "MAYA_MODULE_PATH"):
        if key in env:
            parts = [p for p in env[key].split(os.pathsep)
                     if not re.search(r"[/\\]Maya20\d{2}(?:[/\\]|$)", p, re.I)]
            env[key] = os.pathsep.join(parts)
    env["MAYA_LOCATION"] = str(installation)
    env["PATH"] = str(Path(installation) / "bin") + os.pathsep + env.get("PATH", "")
    return env


def python_command(python, script, arguments=()):
    """Maya's mayapy launcher misdecodes non-ASCII argv on some Windows installs.

    Send an ASCII bootstrap and reconstruct Unicode paths inside Python instead.
    This also works with ordinary Python used for optional postprocessing.
    """
    payload = base64.b64encode(json.dumps([str(script), [str(a) for a in arguments]], ensure_ascii=False).encode("utf-8")).decode("ascii")
    code = ("import base64,json,runpy,sys,os;sys.dont_write_bytecode=True;"
            "p,a=json.loads(base64.b64decode('" + payload + "'));"
            "sys.argv=[p]+a;sys.path.insert(0,os.path.dirname(p));runpy.run_path(p,run_name='__main__')")
    return [str(python), "-B", "-c", code]


class DosAlias:
    """Temporary ASCII drive alias; all bytes stay in the original farm folder.

    Arnold's Windows filename conversion cannot reliably handle a non-ASCII root.
    A DOS-device alias avoids copying any work to another filesystem location.
    """
    def __init__(self, root, record):
        self.root, self.record = Path(root).resolve(), Path(record)
        self.drive, self.target = None, None

    def __enter__(self):
        if os.name != "nt" or str(self.root).isascii():
            return str(self.root)
        import ctypes as c
        from ctypes import wintypes as w
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
        kernel.CreateMutexW.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.ReleaseMutex.argtypes = [w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.DefineDosDeviceW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR]
        kernel.QueryDosDeviceW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
        mutex = kernel.CreateMutexW(None, False, "Local\\PortableMayaFarmDriveAlias")
        if kernel.WaitForSingleObject(mutex, 15000) not in (0, 0x80):
            kernel.CloseHandle(mutex)
            raise TimeoutError("ドライブ別名の割り当てを待機できません")
        self.kernel = kernel
        try:
            stale = read_json(self.record)
            if stale:
                # The caller holds the job run lock; its previous Maya is no longer active.
                kernel.DefineDosDeviceW(1 | 2 | 4 | 8, stale["drive"], stale["target"])
            raw = str(self.root)
            self.target = "\\??\\UNC\\" + raw[2:] if raw.startswith("\\\\") else "\\??\\" + raw
            for letter in reversed("DEFGHIJKLMNOPQRSTUVWXYZ"):
                drive = letter + ":"
                buffer = c.create_unicode_buffer(32768)
                if kernel.QueryDosDeviceW(drive, buffer, len(buffer)):
                    continue
                if kernel.DefineDosDeviceW(1 | 8, drive, self.target):
                    self.drive = drive
                    atomic_json(self.record, {"drive": drive, "target": self.target, "pid": os.getpid()})
                    return drive + "\\"
            raise RuntimeError("日本語パス用の空きドライブ文字がありません")
        finally:
            kernel.ReleaseMutex(mutex)
            kernel.CloseHandle(mutex)

    def __exit__(self, *_):
        if self.drive:
            self.kernel.DefineDosDeviceW(1 | 2 | 4 | 8, self.drive, self.target)
            with contextlib.suppress(OSError):
                self.record.unlink(missing_ok=True)

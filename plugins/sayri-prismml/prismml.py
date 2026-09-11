#!/usr/bin/env python3
"""Prism ML server manager for Sayri — mirrors PrismML-Eng/Bonsai-demo.

Downloads the llama.cpp binaries (release ``prism-b10660-e311ed3``) and the
GGUF models (``prism-ml/{Ternary-,}Bonsai-{1.7B,4B,8B,27B}-gguf``), then runs
``llama-server`` with GPU-aware flags (CUDA / ROCm / Vulkan / CPU) and exposes
a ``/health`` endpoint.

Everything is self-contained: no Sayri imports, no third-party packages.
Both SFTP-less Hugging Face resolution and the GitHub release download use
only :mod:`urllib`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

TAG = os.environ.get("PRISM_TAG", "prism-b10660-e311ed3")
HF_BASE = os.environ.get("PRISM_HF_BASE", "https://huggingface.co")
GH_API = os.environ.get("PRISM_GH_API", "https://api.github.com")

FAMILIES = ["ternary", "bonsai"]
SIZES = ["27B", "8B", "4B", "1.7B"]
DEFAULT_PORT = 8080

Progress = Callable[[Optional[float]], None]
Log = Callable[[list], None]


def _env_path(key: str, default: str) -> Path:
    return Path(os.environ.get(key, default)).expanduser()


def root_dir() -> Path:
    """Where models + binaries + state live (~/.local/share/sayri/prismml)."""
    override = os.environ.get("PRISM_ROOT")
    if override:
        return Path(override).expanduser()
    base = _env_path("SAYRI_STATE_DIR", os.path.join("~", ".local", "share", "sayri"))
    return base / "prismml"


def config_file() -> Path:
    base = _env_path("SAYRI_CONFIG_DIR", os.path.join("~", ".config", "sayri"))
    return base / "prismml.json"


DEFAULTS: dict[str, Any] = {
    "family": "ternary",
    "size": "8B",
    "host": "127.0.0.1",
    "port": DEFAULT_PORT,
    "ctx_size": 4096,
    "params_file": "",
    "gpu_override": "",
}


class Config:
    def __init__(self) -> None:
        self.data: dict = dict(DEFAULTS)
        try:
            if config_file().is_file():
                self.data.update(json.loads(config_file().read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[prismml] config read failed: {exc}\n")
        self.data = {**DEFAULTS, **self.data}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def save(self) -> None:
        config_file().parent.mkdir(parents=True, exist_ok=True)
        config_file().write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def merge(self, updates: dict) -> None:
        for k, v in updates.items():
            if k in DEFAULTS and v not in (None, ""):
                self.data[k] = v


def model_repo(family: str, size: str) -> str:
    prefix = "Ternary-Bonsai" if family == "ternary" else "Bonsai"
    return f"prism-ml/{prefix}-{size}-gguf"


def _download(url: str, dest: Path, progress: Optional[Progress] = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "sayri-prismml/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {url}") from exc


# --------------------------------------------------------------------- models
def fetch_model_files(repo: str) -> list[str]:
    url = f"{HF_BASE}/api/models/{repo}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sayri-prismml/1.0"}), timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    files = [s["rfilename"] for s in data.get("siblings", [])]
    return [f for f in files if f.lower().endswith(".gguf")]


def pick_model_file(repo: str, size: str) -> Optional[str]:
    """Pick the lightest default GGUF for the family.

    Bonsai (1-bit) ships a ``Q1_0`` file; Ternary-Bonsai ships ``PQ2_0`` /
    ``Q2_0_g64`` / ``Q2_0`` / ``F16``. Fall back to any file mentioning the
    size, then to the first available file.
    """
    family = "ternary" if "ternary" in repo.lower() else "bonsai"
    low = size.lower()
    try:
        files = fetch_model_files(repo)
    except Exception:  # noqa: BLE001
        # offline fallback: guess the canonical path
        return (f"Bonsai-{low}-q1_0.gguf" if family == "bonsai"
                else f"Ternary-Bonsai-{low}-q2_0.gguf")
    if not files:
        return None
    preferred = ["q1_0"] if family == "bonsai" else ["pq2_0", "q2_0_g64", "q2_0", "f16"]
    for quant in preferred:
        for f in files:
            if quant in f.lower() and low in f.lower():
                return f
    for f in files:
        if low in f.lower():
            return f
    return files[0]


def model_file(family: str, size: str) -> Path:
    return root_dir() / "models" / f"{family}-{size}.gguf"


def install_model(family: str, size: str, progress: Optional[Progress] = None,
                  log: Optional[Log] = None) -> Path:
    repo = model_repo(family, size)
    name = pick_model_file(repo, size) or f"{family}-{size}-q1_0.gguf"
    dest = model_file(family, size)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        (log or (lambda _m: None))([f"Already present {dest.name} ✓"])
        return dest
    url = f"{HF_BASE}/{repo}/resolve/main/{name}"
    (log or (lambda _m: None))([f"Downloading {repo} · {name}"])
    _download(url, dest, progress=progress)
    return dest

# ------------------------------------------------------------------- binaries
def detect_gpu() -> str:
    override = os.environ.get("PRISM_GPU", "").strip().lower()
    if override:
        return override if override in ("cuda", "rocm", "vulkan", "cpu") else "cpu"
    for probe in ("nvcc", "nvidia-smi"):
        if shutil.which(probe):
            return "cuda"
    if shutil.which("rocminfo"):
        return "rocm"
    if shutil.which("vulkaninfo"):
        return "vulkan"
    return "cpu"


def release_asset() -> tuple[str, str]:
    """Match the llama.cpp binaries published for :data:`TAG`.

    Returns the (asset name, archive extension). Asset names follow the
    upstream release layout: Linux/macOS are ``.tar.gz``, Windows ``.zip``.
    """
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
    gpu = detect_gpu()
    if sysname == "darwin":
        return f"macos-{arch}", "tar.gz"
    if sysname == "linux":
        if arch == "arm64":
            return ("ubuntu-vulkan-arm64" if gpu == "vulkan" else "ubuntu-arm64"), "tar.gz"
        if gpu == "cuda":
            return "linux-cuda-12.4-x64", "tar.gz"
        if gpu == "rocm":
            return "ubuntu-rocm-7.2-x64", "tar.gz"
        if gpu == "vulkan":
            return "ubuntu-vulkan-x64", "tar.gz"
        return "ubuntu-x64", "tar.gz"
    # windows
    if arch == "arm64":
        return "win-cpu-arm64", "zip"
    if gpu == "cuda":
        return "win-cuda-12.4-x64", "zip"
    if gpu == "rocm":
        return "win-hip-radeon-x64", "zip"
    if gpu == "vulkan":
        return "win-vulkan-x64", "zip"
    return "win-cpu-x64", "zip"


def platform_asset() -> str:
    """Human-readable platform+GPU label (see :func:`release_asset`)."""
    asset, _ext = release_asset()
    return asset


def _release_asset_url(asset: str, ext: str) -> str:
    base = os.environ.get("PRISM_RELEASE_URL", "https://github.com/prismml/llama.cpp/releases/download")
    return f"{base}/{TAG}/llama-{TAG}-bin-{asset}.{ext}"


def bin_dir() -> Path:
    return root_dir() / "bins"


def llama_server_bin() -> Path:
    name = "llama-server.exe" if platform.system().lower() == "windows" else "llama-server"
    return bin_dir() / name


def install_binary(progress: Optional[Progress] = None, log: Optional[Log] = None) -> Path:
    dest = llama_server_bin()
    is_win = platform.system().lower() == "windows"
    if dest.is_file() and (is_win or os.access(dest, os.X_OK)):
        (log or (lambda _m: None))([f"Binary already installed ✓"])
        return dest
    asset, ext = release_asset()
    url = _release_asset_url(asset, ext)
    archive = bin_dir() / f"{asset}.{ext}"
    (log or (lambda _m: None))([f"Downloading llama-server binary ({asset})"])
    _download(url, archive, progress=progress)
    if ext == "zip":
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if os.path.basename(name).lower() == dest.name.lower():
                    zf.extract(name, bin_dir())
                    src = bin_dir() / name
                    if str(src.resolve()) != str(dest.resolve()):
                        src.replace(dest)
                    break
    else:
        with tarfile.open(archive) as tf:
            for m in tf.getmembers():
                leaf = os.path.basename(m.name)
                if leaf in ("llama-server", "llama-server.exe"):
                    tf.extract(m, bin_dir())
                    src = bin_dir() / m.name
                    if not src.is_file():
                        src = bin_dir() / leaf
                    if str(src.resolve()) != str(dest.resolve()):
                        src.replace(dest)
                    break
    if not is_win:
        dest.chmod(0o755)
    (log or (lambda _m: None))([f"llama-server installed ✓"])
    return dest


# ------------------------------------------------------------------ the server
class Server:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()

    def _pid_file(self) -> Path:
        return root_dir() / "server.pid"

    @property
    def running(self) -> bool:
        try:
            pid = int(self._pid_file().read_text().strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def pid(self) -> Optional[int]:
        try:
            return int(self._pid_file().read_text().strip())
        except (OSError, ValueError):
            return None

    def start(self, log: Optional[Log] = None, wait=20) -> bool:
        if self.running:
            (log or (lambda _m: None))([f"Already running (PID {self.pid()}) ✓"])
            return True
        binary = llama_server_bin()
        if not (binary.is_file() and (platform.system().lower() == "windows" or os.access(binary, os.X_OK))):
            (log or (lambda _m: None))([f"Binary missing: run 'install' first"])
            return False
        family = self.config.get("family", "ternary")
        size = self.config.get("size", "8B")
        model = model_file(family, size)
        if not model.is_file():
            (log or (lambda _m: None))([f"Model missing {model.name}: run 'download'"])
            return False
        gpu = detect_gpu()
        args = [
            str(binary), "-m", str(model),
            "--host", str(self.config.get("host", "127.0.0.1")),
            "--port", str(self.config.get("port", DEFAULT_PORT)),
            "--ctx-size", str(self.config.get("ctx_size", 4096)),
            "-ngl", "99" if gpu != "cpu" else "0",
        ]
        if self.config.get("params_file"):
            args += ["--paramsfile", self.config["params_file"]]
        args += ["--log-file", str(root_dir() / "server.log")] if os.environ.get("PRISM_DEBUG") else []
        root_dir().mkdir(parents=True, exist_ok=True)
        (log or (lambda _m: None))(["Starting llama-server…"])
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._pid_file().write_text(str(proc.pid))
        deadline = time.time() + wait
        while time.time() < deadline:
            if not self.running:
                return False
            if self.healthy():
                (log or (lambda _m: None))([f"Ready — {self.endpoint()} ✓"])
                return True
            time.sleep(0.5)
        (log or (lambda _m: None))(["Started (no /health response yet)"])
        return True

    def stop(self) -> None:
        pid = self.pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                    time.sleep(0.1)
            except OSError:
                pass
        try:
            self._pid_file().unlink(missing_ok=True)
        except OSError:
            pass

    def endpoint(self) -> str:
        return f"http://{self.config.get('host', '127.0.0.1')}:{self.config.get('port', DEFAULT_PORT)}"

    def healthy(self) -> bool:
        try:
            url = f"{self.endpoint()}/health"
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = resp.read().decode("utf-8", "replace")
            return resp.status == 200 and '"ok"' in data
        except Exception:  # noqa: BLE001
            return False

    def status_payload(self) -> dict:
        cfg = self.config
        model = model_file(cfg.get("family", "ternary"), cfg.get("size", "8B"))
        return {
            "running": self.running,
            "pid": self.pid(),
            "gpu": detect_gpu(),
            "family": cfg.get("family"), "size": cfg.get("size"),
            "endpoint": self.endpoint(),
            "health": self.healthy(),
            "binary": llama_server_bin().is_file(),
            "model": model.is_file(),
            "model_path": str(model),
            "port": cfg.get("port"),
        }

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
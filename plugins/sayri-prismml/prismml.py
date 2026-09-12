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

import hashlib
import json
import os
import platform
import re
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

TAG = os.environ.get("PRISM_TAG", "prism-b10683-d8f26ee")
HF_BASE = os.environ.get("PRISM_HF_BASE", "https://huggingface.co")
GH_API = os.environ.get("PRISM_GH_API", "https://api.github.com")

FAMILIES = ["ternary", "bonsai"]
SIZES = ["27B", "8B", "4B", "1.7B"]
DEFAULT_PORT = 8080

# Quantization levels available per family (slug → human label). Bonsai is a
# 1-bit model (Q1_0 only); Ternary-Bonsai ships PQ2_0 / Q2_0_g64 / Q2_0 / F16.
QUANTS: dict[str, list[str]] = {
    "ternary": ["pq2_0", "q2_0_g64", "q2_0", "f16"],
    "bonsai": ["q1_0"],
}
QUANT_LABELS: dict[str, str] = {
    "pq2_0": "PQ2_0 — lightest ternary (recommended)",
    "q2_0_g64": "Q2_0_g64 — grouped quant, good balance",
    "q2_0": "Q2_0 — classic 2-bit ternary",
    "f16": "F16 — best quality, largest",
    "q1_0": "Q1_0 — 1-bit Bonsai (only option)",
}
DEFAULT_QUANT: dict[str, str] = {"ternary": "pq2_0", "bonsai": "q1_0"}

# sha256 digests of every llama-server asset we can download, taken from the
# GitHub release (asset .digest) for `TAG`. Unknown/unsigned assets are refused.
BINARY_SHA256: dict[str, str] = {
    "macos-arm64": "0ae163ca2c9cce92470316ed743f76985beea4d5cf31b8dc546711cf6fc8dd35",
    "macos-x64": "29e16154c9feab99fa5b4d2a5cced3d86557ce3f37f0597ebdf0825fc77c680b",
    "ubuntu-x64": "68ff1c860aafc1fc45bb18e90e19147e3fd086bd6724da088418e874fcc004c2",
    "ubuntu-arm64": "9cc6edaae0fb60a3c5a36cea25bd8449866a5fba61f26bff9f68ac0371d6caa8",
    "ubuntu-vulkan-x64": "0cf2c404ad89ac42c46bbe0e3288f4a16b00a4bbed1807e5e1d8c7f6294dc2ce",
    "ubuntu-vulkan-arm64": "faef2c408c9506bdd74895ac5531a06b71e8f51f926905a9f2cad0be261511ac",
    "ubuntu-rocm-7.2-x64": "019741d3b585a6aca360ff3da8a001c56fa0696c39ab8ca4affa3dc4cb5212da",
    "linux-cuda-12.4-x64": "fd437bf65ce449c77a40edee99c61365f7d548120a6c63657744d4971c9b80b6",
    "win-cpu-x64": "3d68c36d5743c06e7334a2c2da2cebf2b4c4c230f706db69ef095a7a1419a8e0",
    "win-cpu-arm64": "191ecca1b1eea0702038f56b88a6e563d7d74051456c175415da6cffc209598c",
    "win-cuda-12.4-x64": "07a4c945779bda6b0e12e51ad97c55858e126ea903bfdb3053a16cd29d2f2257",
    "win-vulkan-x64": "5559fd0903975a83929bbf76cb9b895a5fcb2f8af449d53563f837890ebc8476",
    "win-hip-radeon-x64": "1c14bfee085128a74c52fbd5dda9564ddc67f63e1d64e166465f23f76c6812b4",
}

Progress = Callable[[Optional[float]], None]
Log = Callable[[list], None]

_HOST_RE = re.compile(r"^[A-Za-z0-9_.\-:\[\]]+$")


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
    "quant": "",
    "host": "127.0.0.1",
    "port": DEFAULT_PORT,
    "ctx_size": 4096,
    "params_file": "",
    "gpu_override": "",
    "enabled": True,
}


def effective_quant(family: str, quant: str = "", default: bool = False) -> str:
    """Quantization slug actually used. Fallback per family when ``quant`` is
    empty/invalid: ``DEFAULT_QUANT`` (unless ``default`` is False and ``quant``
    is empty → keep the legacy single-file name)."""
    if quant and quant in QUANTS.get(family, []):
        return quant
    if default:
        return DEFAULT_QUANT.get(family, "pq2_0")
    return quant or ""


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

    def as_bool(self, key: str, default: bool = False) -> bool:
        """Config value as a bool, tolerant of string forms from the UI."""
        v = self.data.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "yes", "on", "si")

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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, want: str, label: str) -> None:
    """Assert the file matches the pinned sha256; delete it otherwise."""
    got = sha256_file(path)
    if got.lower() != str(want).lower():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Integrity check failed for {label}: expected {want}, got {got}")


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


def fetch_model_oid(repo: str, filename: str) -> Optional[str]:
    """LFS sha256 of a GGUF file from the HF tree API (None if unknown)."""
    url = f"{HF_BASE}/api/models/{repo}/tree/main?recursive=true"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sayri-prismml/1.0"}), timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for e in data:
        if e.get("path") == filename and isinstance(e.get("lfs"), dict):
            return e["lfs"].get("oid")
    return None


def pick_model_file(repo: str, size: str, quant: str = "") -> Optional[str]:
    """Pick the GGUF for the family/size, honouring an explicit quantization.

    Bonsai (1-bit) ships a ``Q1_0`` file; Ternary-Bonsai ships ``PQ2_0`` /
    ``Q2_0_g64`` / ``Q2_0`` / ``F16``. With ``quant`` set, only that exact
    quantization is matched; otherwise the lightest preferred variant wins.
    Fall back to any file mentioning the size, then to the first file.
    """
    family = "ternary" if "ternary" in repo.lower() else "bonsai"
    low = size.lower()
    quant = effective_quant(family, quant, default=True)

    def _exact(name: str) -> bool:
        n = name.lower()
        if f"-{quant}.gguf" in n:
            return True
        if quant == "q2_0":
            return "pq2_0" not in n and "q2_0_g64" not in n and quant in n
        return quant in n

    try:
        files = fetch_model_files(repo)
    except Exception:  # noqa: BLE001
        # offline fallback: guess the canonical path
        if family == "bonsai":
            return f"Bonsai-{low}-{quant}.gguf"
        return f"Ternary-Bonsai-{low}-{quant.upper() if quant in ('f16',) else quant.upper()}.gguf"
    if not files:
        return None
    if quant:
        for f in files:
            if _exact(f) and low in f.lower():
                return f
    preferred = ["q1_0"] if family == "bonsai" else ["pq2_0", "q2_0_g64", "q2_0", "f16"]
    for q in preferred:
        for f in files:
            if q in f.lower() and low in f.lower():
                return f
    for f in files:
        if low in f.lower():
            return f
    return files[0]


def _quant_display(q: str) -> str:
    """Human label for a quant slug; empty keeps the legacy name."""
    return QUANT_LABELS.get(q, q or "")


def model_file(family: str, size: str, quant: str = "") -> Path:
    quant = effective_quant(family, quant)
    if quant:
        return root_dir() / "models" / f"{family}-{size}-{quant}.gguf"
    return root_dir() / "models" / f"{family}-{size}.gguf"


def resolve_model_file(family: str, size: str, quant: str = "") -> Optional[Path]:
    """The GGUF that actually exists for this family/size.

    Prefers the explicit quantization, then the family default quant, then
    ``Q1_0`` and finally *any* quantized ``{family}-{size}-*`` file. The legacy
    base name (``{family}-{size}.gguf``) is only used as a last resort, so a
    stale/incomplete single-file download can never shadow a good quantized
    model.
    """
    models = root_dir() / "models"
    cands: list[Path] = []
    if quant:
        cands.append(model_file(family, size, quant))
    dq = DEFAULT_QUANT.get(family, "pq2_0")
    if dq:
        cands.append(model_file(family, size, dq))
    cands.append(model_file(family, size, "q1_0"))
    for c in cands:
        if c.is_file() and c.stat().st_size > 1_000_000:
            return c
    try:
        found = sorted(models.glob(f"{family}-{size}-*.gguf")) if models.is_dir() else []
    except Exception:  # noqa: BLE001
        found = []
    for f in found:
        if f.is_file() and f.stat().st_size > 1_000_000:
            return f
    legacy = models / f"{family}-{size}.gguf"
    if legacy.is_file() and legacy.stat().st_size > 1_000_000:
        return legacy
    return None


def install_model(family: str, size: str, quant: str = "",
                  progress: Optional[Progress] = None,
                  log: Optional[Log] = None) -> Path:
    repo = model_repo(family, size)
    name = pick_model_file(repo, size, quant) or f"{family}-{size}-q1_0.gguf"
    dest = model_file(family, size, quant)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        (log or (lambda _m: None))([f"Already present {dest.name} ✓"])
        return dest
    url = f"{HF_BASE}/{repo}/resolve/main/{name}"
    (log or (lambda _m: None))([f"Downloading {repo} · {name}"])
    _download(url, dest, progress=progress)
    if os.environ.get("PRISM_SKIP_INTEGRITY", "").strip().lower() not in ("1", "true", "yes"):
        oid = fetch_model_oid(repo, name)
        if oid:
            verify_sha256(dest, oid, f"{repo} · {name}")
        else:
            (log or (lambda _m: None))(["⚠ No checksum available — integrity not verified"])
    return dest

# ------------------------------------------------------------------- binaries
def _cuda_runtime_available() -> bool:
    """True only if the CUDA runtime .so sonames are actually loadable."""
    try:
        out = subprocess.check_output(
            ["ldconfig", "-p"], stderr=subprocess.DEVNULL, text=True
        )
        hay = out
    except Exception:  # noqa: BLE001
        hay = ""
    return all(hay.find(f"lib{s}.so.12") >= 0 for s in ("cudart", "cublas"))


def detect_gpu() -> str:
    override = os.environ.get("PRISM_GPU", "").strip().lower()
    if override:
        return override if override in ("cuda", "rocm", "vulkan", "cpu") else "cpu"
    for probe in ("nvcc", "nvidia-smi"):
        if shutil.which(probe):
            # a driver without the runtime libs cannot load the CUDA build
            return "cuda" if _cuda_runtime_available() else "cpu"
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
    base = os.environ.get("PRISM_RELEASE_URL", "https://github.com/PrismML-Eng/llama.cpp/releases/download")
    return f"{base}/{TAG}/llama-{TAG}-bin-{asset}.{ext}"


def bin_dir() -> Path:
    return root_dir() / "bins"


def llama_server_bin() -> Path:
    name = "llama-server.exe" if platform.system().lower() == "windows" else "llama-server"
    return bin_dir() / name


def _flatten_bin_dir(bin_dir_: Path) -> None:
    """Move everything from nested release dirs up to ``bin_dir_``/ root.

    The PrismML release archives ship a single top-level folder
    (e.g. ``llama-prism-b10683-d8f26ee/``) that contains the binaries and the
    ``*.so`` libraries they dlopen. We move every entry (files and symlinks)
    up one level, then recreate soname symlinks (``libX.so.N``) from the fully
    versioned names (``libX.so.N.M``) that the tarball may only have as
    relative symlinks between one another.
    """
    subdirs = [p for p in bin_dir_.iterdir() if p.is_dir() and not p.name.startswith(".")]
    for sub in subdirs:
        for entry in list(sub.rglob("*")):
            if entry.is_dir():
                continue
            flat = bin_dir_ / entry.name
            if entry.parent != bin_dir_ and not flat.exists():
                try:
                    entry.rename(flat)
                except OSError:
                    pass
    import re as _re
    for lib in list(bin_dir_.glob("lib*.so.*")):
        m = _re.match(r"^(lib.*\.so)\.(\d+)\.(\d+)", lib.name)
        if not m or not lib.is_file():
            continue
        ver = f"{m.group(1)}.{m.group(2)}"
        plain = m.group(1)
        for target in (ver, plain):
            ln = bin_dir_ / target
            if not ln.exists() and not ln.is_symlink():
                try:
                    ln.symlink_to(lib.name)
                except OSError:
                    pass


def install_binary(progress: Optional[Progress] = None, log: Optional[Log] = None) -> Path:
    dest = llama_server_bin()
    is_win = platform.system().lower() == "windows"
    if dest.is_file() and (is_win or os.access(dest, os.X_OK)):
        (log or (lambda _m: None))([f"Binary already installed ✓"])
        return dest
    asset, ext = release_asset()
    url = _release_asset_url(asset, ext)
    archive = bin_dir() / f"{asset}.{ext}"
    want = BINARY_SHA256.get(asset)
    if not want:
        raise RuntimeError(f"No pinned checksum for asset {asset} — refusing to install")
    (log or (lambda _m: None))([f"Downloading llama-server binary ({asset})"])
    _download(url, archive, progress=progress)
    verify_sha256(archive, want, f"llama-server binary ({asset})")
    if ext == "zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(bin_dir())
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(bin_dir())
    _flatten_bin_dir(bin_dir())
    # locate llama-server, respecting Windows exe name
    dest_candidates = [dest, bin_dir() / "llama-server", bin_dir() / "llama-server.exe"]
    for candidate in dest_candidates:
        if candidate.is_file():
            if candidate != dest:
                candidate.replace(dest)
            break
    else:
        raise RuntimeError("llama-server binary not found in archive")
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
        quant = self.config.get("quant", "") or ""
        if family not in FAMILIES:
            (log or (lambda _m: None))([f"Invalid family '{family}' (choose one of: {', '.join(FAMILIES)})"])
            return False
        if size not in SIZES:
            (log or (lambda _m: None))([f"Invalid size '{size}' (choose one of: {', '.join(SIZES)})"])
            return False
        if quant and quant not in QUANTS.get(family, []):
            (log or (lambda _m: None))([f"Invalid quant '{quant}' for family '{family}' (use: {', '.join(QUANTS[family])})"])
            return False
        model = resolve_model_file(family, size, quant)
        if model is None:
            name = model_file(family, size, quant).name
            (log or (lambda _m: None))([f"Model missing {name}: run 'download'"])
            return False
        host = str(self.config.get("host", "127.0.0.1"))
        if not _HOST_RE.match(host):
            (log or (lambda _m: None))([f"Invalid host '{host}'"])
            return False
        try:
            port = int(self.config.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            (log or (lambda _m: None))(["Invalid port; expected an integer"])
            return False
        if not (1 <= port <= 65535):
            (log or (lambda _m: None))([f"Invalid port {port} (must be 1–65535)"])
            return False
        try:
            ctx = int(self.config.get("ctx_size", 4096))
        except (TypeError, ValueError):
            ctx = 4096
        params_file = self.config.get("params_file") or ""
        if params_file and not os.path.isfile(params_file):
            (log or (lambda _m: None))([f"Params file not found: {params_file}"])
            return False
        gpu = detect_gpu()
        args = [
            str(binary), "-m", str(model),
            "--host", host,
            "--port", str(port),
            "--ctx-size", str(max(128, ctx)),
            "-ngl", "99" if gpu != "cpu" else "0",
        ]
        if params_file:
            args += ["--paramsfile", params_file]
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
        family = cfg.get("family", "ternary")
        size = cfg.get("size", "8B")
        quant = effective_quant(family, cfg.get("quant", "") or "", default=True)
        model = model_file(family, size, quant)
        return {
            "running": self.running,
            "pid": self.pid(),
            "gpu": detect_gpu(),
            "family": family, "size": size, "quant": quant,
            "quant_label": _quant_display(quant),
            "endpoint": self.endpoint(),
            "health": self.healthy(),
            "binary": llama_server_bin().is_file(),
            "model": model.is_file(),
            "model_path": str(model),
            "port": cfg.get("port"),
            "enabled": cfg.as_bool("enabled", True),
        }

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
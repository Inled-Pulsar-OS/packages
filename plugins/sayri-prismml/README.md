# sayri-prismml — Prism ML Server (Bonsai / Ternary-Bonsai demo)

Runs a Prism ML model 100 % locally like
[PrismML-Eng/Bonsai-demo](https://github.com/PrismML-Eng/Bonsai-demo): detects
your GPU (CUDA / ROCm / Vulkan / CPU), downloads the `llama-server` tools and
the GGUF model, and serves an OpenAI-compatible API at `http://127.0.0.1:8080`
with a `GET /health` health endpoint.

Feedback on the desktop (web panel) and in the terminal (xui wizard) with the
same flow as the Sayri "welcome wizard".

## Quick start

    python3 gateway.py wizard      # wizard: install, download and start
    python3 gateway.py status      # server / GPU / model status
    python3 gateway.py serve       # web panel at http://127.0.0.1:8756
    python3 gateway.py open        # open the llama-server chat

Through the Sayri daemon (if you have it): install the plugin as usual and run
`sayri-prismml <command>` (`gateway.py` also works standalone from its dir).

## Commands

| Command                    | What it does                                                   |
| -------------------------- | -------------------------------------------------------------- |
| `wizard`                   | interactive wizard (family → size → runtime → downloads)       |
| `status`                   | server, GPU, binary, model, health status                      |
| `install`                  | download the `llama-server` binary for your platform           |
| `download [family] [size]` | download the GGUF model (Q1_0 / PQ2_0)                |
| `start [--port N]`         | start llama-server with the GPU flags                          |
| `stop` / `restart`         | stop / restart llama-server                                    |
| `run`                      | download what is missing and keep the server in the foreground |
| `serve [--port N]`         | wizard web panel (live status + xui)                           |
| `open`                     | open the server endpoint in the browser                        |
| `info`                     | paths, detected GPU, repos                                     |

## Models and hardware

* Families: `ternary` (recommended) and `bonsai`.
* Sizes: `27B`, `8B`, `4B`, `1.7B` — every family × size combination is a
  supported model (8 in total).
* HF repos: `prism-ml/Bonsai-{size}-gguf` and `prism-ml/Ternary-Bonsai-{size}-gguf`.
  Bonsai ships a `*-Q1_0.gguf`; Ternary-Bonsai ships
  `*-PQ2_0.gguf`, `*-Q2_0_g64.gguf`, `*-Q2_0.gguf` and `*-F16.gguf`
  (the lightest available quant is picked automatically).
* Binaries: release `prism-b10660-e311ed3`, assets
  `llama-{TAG}-bin-{macos-arm64|macos-x64|ubuntu-x64|ubuntu-arm64|ubuntu-vulkan-x64|ubuntu-vulkan-arm64|linux-cuda-12.4-x64|ubuntu-rocm-7.2-x64}.tar.gz`
  and Windows `...-bin-{win-cpu-x64|win-cpu-arm64|win-cuda-12.4-x64|win-vulkan-x64|win-hip-radeon-x64}.zip`.

The GPU is chosen automatically (nvcc/nvidia-smi → CUDA, rocminfo → ROCm,
vulkaninfo → Vulkan, else CPU); `-ngl 99` for GPU, `-ngl 0` for CPU.

## Paths and configuration

* Config: `~/.config/sayri/prismml.json` (`family`, `size`, `host`, `port`,
  `ctx_size`, `params_file`, `gpu_override`).
* Data: `~/.local/share/sayri/prismml/` (`models/`, `bins/`, `server.pid`,
  `server.log`).
* Environment: `PRISM_ROOT`, `PRISM_TAG`, `PRISM_GPU`,
  `PRISM_RELEASE_URL`, `PRISM_HF_BASE`, `PRISM_DEBUG`.

## Notes

* `27B` needs ~16 GB of RAM minimum; `1.7B` runs even on CPU.
* If `serve` is used without a GPU/downloads, the panel shows the real status
  and lets you continue via CLI.
* The "Prism ML" wizard section shares the system `xui` engine (vendored in
  `xui.py` so the plugin stays self-contained).
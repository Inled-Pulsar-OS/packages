#!/usr/bin/env python3
"""Sayri · Prism ML — local LLM server plugin (Bonsai / Ternary-Bonsai demo).

CLI-only entrypoint. The xui UI host, the browser page and the HTTP panel live
in ``ui.py`` (decoupled: no argparse there); the model/server logic lives in
``prismml.py``.

    python3 gateway.py wizard        # TUI xui
    python3 gateway.py serve         # browser panel (HTTP)
    python3 gateway.py status …      # plain CLI commands
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from typing import Any, Optional

try:
    import prismml
    from ui import run_tui_guard, serve_http  # type: ignore  # same plugin dir
except ImportError as exc:  # pragma: no cover - only when run outside the plugin dir
    sys.stderr.write(f"[prismml] plugin dependencies missing (run from the plugin dir): {exc}\n")
    sys.exit(1)


def _progress(label: str) -> tuple:
    def progress(v: Optional[float]) -> None:
        sys.stdout.write("\r  {0} {1:3d}%  ".format(label, int((v or 0) * 100)))
        sys.stdout.flush()

    def log(lines: list) -> None:
        sys.stdout.write("\r" + " " * 40 + "\r")
        for line in lines:
            print("  ·", line)

    return progress, log


def cmd_status(_args) -> int:
    st = prismml.Server().status_payload()
    print(f"family:       {st['family']} / {st['size']} / {st['quant'].upper()}")
    print(f"GPU:          {st['gpu']}")
    print(f"auto-start:   {'enabled' if st['enabled'] else 'disabled'}")
    print(f"binary:       {'installed' if st['binary'] else 'pending'}")
    print(f"model:        {'downloaded' if st['model'] else 'pending'}")
    print(f"server:       {'running (PID %s)' % st['pid'] if st['running'] else 'stopped'}")
    print(f"health:       {'ok' if st['health'] else 'no'}")
    print(f"endpoint:     {st['endpoint']}")
    print(f"model path:   {st['model_path']}")
    return 0


def cmd_install(_args) -> int:
    p, l = _progress("llama-server binary")
    try:
        prismml.install_binary(progress=p, log=l)
        print("\n  binary ready ✓")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n  error: {exc}")
        return 1


def cmd_download(args) -> int:
    family = args.family or prismml.Config().get("family", "ternary")
    size = args.size or prismml.Config().get("size", "8B")
    quant = args.quant or prismml.Config().get("quant", "") or ""
    p, l = _progress(f"model {family}/{size}/{quant or 'auto'}")
    try:
        prismml.install_model(family, size, quant, progress=p, log=l)
        print("\n  model ready ✓")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n  error: {exc}")
        return 1


def cmd_start(args) -> int:
    cfg = prismml.Config()
    if args.port:
        try:
            cfg.set("port", int(args.port))
            cfg.save()
        except ValueError:
            pass
    if args.quant:
        cfg.set("quant", args.quant)
        cfg.save()

    ok = prismml.Server(cfg).start(log=lambda lines: [print("  ·", ln) for ln in lines])
    st = prismml.Server(cfg).status_payload()
    print("  server " + ("running ✓" if ok and st["running"] else "unconfirmed / failed"))
    return 0 if ok and st["running"] else 1


def cmd_stop(_args) -> int:
    prismml.Server().stop()
    print("  server stopped.")
    return 0


def cmd_restart(args) -> int:
    prismml.Server().stop()
    return cmd_start(args)


def cmd_run(args) -> int:
    cfg = prismml.Config()
    family = cfg.get("family", "ternary")
    size = cfg.get("size", "8B")
    quant = cfg.get("quant", "") or ""
    if not prismml.llama_server_bin().is_file():
        print("Installing binary…", flush=True)
        prismml.install_binary(progress=lambda v: print(f"\r  {int((v or 0)*100):3d}%", end="", flush=True))
        print(flush=True)
    if not prismml.model_file(family, size, quant).is_file():
        print("Downloading model…", flush=True)
        prismml.install_model(family, size, quant,
                              progress=lambda v: print(f"\r  {int((v or 0)*100):3d}%", end="", flush=True))
        print(flush=True)
    if not prismml.Server(cfg).start():
        print("  the server did not respond; see ~/.local/share/sayri/prismml/server.log")
        return 1
    ep = prismml.Server(cfg).endpoint()
    print(f"\n  {ep} — llama-server running. Ctrl+C stops the panel.")
    try:
        while True:
            h = "ok" if prismml.Server(cfg).healthy() else "no"
            sys.stdout.write(f"\r  health: {h}  (PID {prismml.Server(cfg).pid()})   ")
            sys.stdout.flush()
            time.sleep(3)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_open(_args) -> int:
    webbrowser.open(prismml.Server().endpoint())
    return 0


def cmd_info(_args) -> int:
    cfg = prismml.Config()
    print(f"binary tag:     {prismml.TAG}")
    print(f"detected gpu:   {prismml.detect_gpu()}")
    print(f"platform:       {prismml.platform_asset()}")
    print(f"config:         {prismml.config_file()}")
    print(f"root:           {prismml.root_dir()}")
    print(f"binary:         {prismml.llama_server_bin()}")
    print(f"family/size:    {cfg.get('family')} / {cfg.get('size')}")
    print(f"quant:          {cfg.get('quant') or 'auto'}")
    print(f"model file:     {prismml.model_file(cfg.get('family'), cfg.get('size'), cfg.get('quant', '') or '')}")
    print(f"HF repo:        {prismml.model_repo(cfg.get('family'), cfg.get('size'))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sayri-prismml", description="Prism ML local LLM server")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wizard", help="interactive wizard (install, download, start)")
    sub.add_parser("status", help="server, model and GPU status")
    sub.add_parser("install", help="download the llama-server tools")
    dl = sub.add_parser("download", help="download the GGUF model")
    dl.add_argument("family", nargs="?", choices=prismml.FAMILIES)
    dl.add_argument("size", nargs="?", choices=prismml.SIZES)
    dl.add_argument("--quant", choices=sum(prismml.QUANTS.values(), []),
                    help="explicit quantization (default: auto per family)")
    st = sub.add_parser("start", help="start llama-server")
    st.add_argument("--port", type=int)
    st.add_argument("--quant", choices=sum(prismml.QUANTS.values(), []),
                    help="explicit quantization (default: auto per family)")
    rt = sub.add_parser("run", help="download what is missing and run llama-server in the foreground")
    rt.add_argument("--quant", choices=sum(prismml.QUANTS.values(), []),
                    help="explicit quantization (default: auto per family)")
    sub.add_parser("stop", help="stop llama-server")
    sub.add_parser("restart", help="restart llama-server")
    sub.add_parser("open", help="open the endpoint in the browser")
    sub.add_parser("info", help="paths, GPU and repos")
    sv = sub.add_parser("serve", help="web panel with the wizard (xui)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8756)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "wizard":
        return run_tui_guard()
    if args.cmd == "serve":
        serve_http(host=args.host or "127.0.0.1", port=args.port or 8756)
        return 0
    table = {
        "status": cmd_status,
        "install": cmd_install,
        "download": cmd_download,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "run": cmd_run,
        "open": cmd_open,
        "info": cmd_info,
    }
    return table[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
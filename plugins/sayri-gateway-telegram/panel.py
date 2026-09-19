#!/usr/bin/env python3
"""Telegram Gateway · thin CLI — drives the decoupled xui UI (``ui.py``).

    python3 panel.py wizard              # TUI xui
    python3 panel.py serve               # browser panel (HTTP)
    python3 panel.py status              # daemon / token / PIN status
    python3 panel.py token <TOKEN>       # saves the token in secrets.json
    python3 panel.py pin [--rotate]      # shows / rotates the PIN
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

try:
    from ui import (  # type: ignore  # same plugin dir
        get_pin, get_secret, rotate_pin, run_tui_guard, serve_http,
        set_secret, status_payload,
    )
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(f"[telegram] plugin dependencies missing (run from the plugin dir): {exc}\n")
    sys.exit(1)


def cmd_status(_args) -> int:
    st = status_payload()
    print(f"token:       {'configured ✓' if st['token'] else 'pending ✗'}")
    print(f"daemon:      {'running (PID %s)' % st['pid'] if st['running'] else 'stopped'}")
    print(f"PIN:         {st['pin_value'] or 'not generated'}")
    print(f"config dir:  {st['config_dir']}")
    return 0


def cmd_token(args) -> int:
    set_secret(str(args.value))
    print("  token saved in secrets.json ✓ (the daemon will use it if there is no TELEGRAM_BOT_TOKEN variable)")
    return 0


def cmd_pin(args) -> int:
    if args.rotate:
        print(f"  new PIN: {rotate_pin()}")
    else:
        print(f"  PIN: {get_pin() or 'not generated (use --rotate)'}")
    return 0


def cmd_info(_args) -> int:
    from gateway import INSTANCE_ID
    print(f"instance:    {INSTANCE_ID}")
    print(f"current token: {'yes' if get_secret() else 'not configured'}")
    st = status_payload()
    print(f"config dir:  {st['config_dir']}")
    print(f"daemon:      {st['running']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sayri-telegram", description="Telegram bot gateway for Sayri")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wizard", help="xui wizard in the terminal")
    sv = sub.add_parser("serve", help="xui web panel")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8756)
    sub.add_parser("status", help="daemon / token / PIN status")
    tk = sub.add_parser("token", help="save the bot token in secrets.json")
    tk.add_argument("value")
    pn = sub.add_parser("pin", help="show / rotate the pairing PIN")
    pn.add_argument("--rotate", action="store_true")
    sub.add_parser("info", help="paths and status")
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
        "token": cmd_token,
        "pin": cmd_pin,
        "info": cmd_info,
    }
    return table[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
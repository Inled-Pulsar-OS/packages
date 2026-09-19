#!/usr/bin/env python3
"""Discord Gateway · thin CLI — drives the decoupled xui UI (``ui.py``).

    python3 panel.py wizard              # TUI xui
    python3 panel.py serve               # browser panel (HTTP)
    python3 panel.py status              # daemon / token / PIN status
    python3 panel.py token <TOKEN>       # saves the token in secrets.json
    python3 panel.py pin [--rotate]      # shows / rotates the PIN
    python3 panel.py guests <on|off>     # enables / disables guests
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

try:
    from ui import (  # type: ignore  # same plugin dir
        delete_secret, get_pin, get_secret, rotate_pin, run_tui_guard, serve_http,
        set_guests, set_secret, status_payload,
    )
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(f"[discord] plugin dependencies missing (run from the plugin dir): {exc}\n")
    sys.exit(1)


def cmd_status(_args) -> int:
    st = status_payload()
    print(f"token:       {'configured ✓' if st['token'] else 'pending ✗'}")
    print(f"daemon:      {'running (PID %s)' % st['pid'] if st['running'] else 'stopped'}")
    print(f"guests:      {'allowed' if st['guests'] else 'denied'}")
    print(f"PIN:         {st['pin_value'] or 'not generated'}")
    print(f"config dir:  {st['config_dir']}")
    return 0


def cmd_token(args) -> int:
    set_secret(str(args.value))
    print("  token saved in secrets.json ✓")
    return 0


def cmd_rotate(args) -> int:
    old = get_secret()
    if old:
        delete_secret()
        print("  old token removed from secrets.json ✓")
    new_tok = args.value.strip() if args.value else None
    if not new_tok:
        new_tok = input("  paste new Discord bot token: ").strip()
    if not new_tok:
        print("  no token provided, nothing to save", file=sys.stderr)
        return 1
    set_secret(new_tok)
    print("  new token saved ✓ — restart the gateway to apply")
    return 0


def cmd_pin(args) -> int:
    if args.rotate:
        print(f"  new PIN: {rotate_pin()}")
    else:
        print(f"  PIN: {get_pin() or 'not generated (use --rotate)'}")
    return 0


def cmd_guests(args) -> int:
    set_guests(args.state == "on")
    print(f"  guests: {'allowed' if args.state == 'on' else 'denied'}")
    return 0


def cmd_info(_args) -> int:
    print(f"instance:    {__import__('gateway', fromlist=['INSTANCE_ID']).INSTANCE_ID}")
    print(f"current token: {'yes' if get_secret() else 'not configured'}")
    st = status_payload()
    print(f"config dir:  {st['config_dir']}")
    print(f"daemon:      {st['running']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sayri-discord", description="Discord bot gateway for Sayri")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wizard", help="xui wizard in the terminal")
    sv = sub.add_parser("serve", help="xui web panel")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8756)
    sub.add_parser("status", help="daemon / token / PIN / guests status")
    tk = sub.add_parser("token", help="save the bot token in secrets.json")
    tk.add_argument("value")
    rt = sub.add_parser("rotate", help="delete the old token and save a new one")
    rt.add_argument("value", nargs="?")
    pn = sub.add_parser("pin", help="show / rotate the pairing PIN")
    pn.add_argument("--rotate", action="store_true")
    gs = sub.add_parser("guests", help="enable or disable channel guests")
    gs.add_argument("state", choices=["on", "off"])
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
        "rotate": cmd_rotate,
        "pin": cmd_pin,
        "guests": cmd_guests,
        "info": cmd_info,
    }
    return table[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""Telegram Gateway · UI layer — cross-renderer xui host + HTTP panel.

Decoupled from the CLI: no argparse, no command parsing. Exposes the wizard
host (render/dispatch), the terminal runner, the browser page and a local
HTTP panel. The CLI lives in ``panel.py`` and the daemon in ``gateway.py``.

State is read/written through the same files the daemon uses:
``~/.config/sayri/secrets.json`` (token, key TELEGRAM_BOT_TOKEN) and
``pairing_pin_<instance>.json``.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from xui import (  # type: ignore  # vendored inside the plugin dir
    TaskRunner, button, check, entry, note, run_cli, screen, sub, text,
    wizard_page,
)

try:
    from gateway import DEFAULT_CONFIG_DIR, INSTANCE_ID, SHARED_PIN_FILE  # type: ignore
except Exception:  # noqa: BLE001 - fall back to the default names when run alone
    INSTANCE_ID = os.environ.get("SAYRI_GATEWAY_INSTANCE_ID", "default")
    DEFAULT_CONFIG_DIR = Path.home() / ".config" / "sayri"
    SHARED_PIN_FILE = DEFAULT_CONFIG_DIR / f"pairing_pin_{INSTANCE_ID}.json"

PID_FILE = Path(os.environ.get("SAYRI_PID_FILE", str(DEFAULT_CONFIG_DIR / f"gateway_{INSTANCE_ID}.pid")))
SECRETS_FILE = DEFAULT_CONFIG_DIR / "secrets.json"
TOKEN_KEY = "TELEGRAM_BOT_TOKEN"

PANEL_TITLE = "Telegram Gateway · Sayri"


# ------------------------------------------------------------------ backend
def get_secret(key: str = TOKEN_KEY) -> str:
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        item = data.get("secrets", {}).get(key, {})
        return str(item.get("value", "") if isinstance(item, dict) else item)
    except Exception:  # noqa: BLE001
        return ""


def set_secret(value: str, key: str = TOKEN_KEY) -> None:
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8")) if SECRETS_FILE.is_file() else {}
    except Exception:  # noqa: BLE001
        data = {}
    secrets = data.setdefault("secrets", {})
    secrets[key] = {"value": value}
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:  # noqa: BLE001
        return None


def get_pin() -> str:
    try:
        data = json.loads(SHARED_PIN_FILE.read_text(encoding="utf-8"))
        return str(data.get("pin", ""))
    except Exception:  # noqa: BLE001
        return ""


def rotate_pin() -> str:
    raw = f"{random.randint(100000, 999999)}"
    SHARED_PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SHARED_PIN_FILE.write_text(json.dumps({
        "pin": raw,
        "created_at": time.time(),
        "expires_at": time.time() + 86400,
    }, indent=2), encoding="utf-8")
    return raw


def status_payload() -> dict:
    pid = get_pid()
    return {
        "token": bool(get_secret()),
        "running": is_pid_alive(pid),
        "pid": pid,
        "pin": bool(get_pin()),
        "pin_value": get_pin(),
        "config_dir": str(DEFAULT_CONFIG_DIR),
    }


# ------------------------------------------------------------------ wizard
class TelegramWizard:
    """One wizard host for the terminal, the browser panel and the HTTP panel."""

    def __init__(self) -> None:
        self.idx = 0
        self.val: dict = {"token": get_secret()}

    def render(self) -> Optional[dict]:
        if self.idx == 1:
            return self._configure_screen()
        if self.idx == 2:
            return self._pin_screen()
        return self._home_screen()

    def _home_screen(self) -> dict:
        st = status_payload()
        lines = [
            f"bot token: {'configured ✓' if st['token'] else 'pending ✗'}",
            f"daemon: {'running (PID %s)' % st['pid'] if st['running'] else 'stopped'}",
            f"pairing PIN: {'available' if st['pin'] else 'not generated'}",
        ]
        footer = [
            button("configure", "Configure token", kind="primary"),
            button("pin", "Show PIN"),
            button("quit", "Close", kind="secondary"),
        ]
        return screen(
            "Telegram Gateway · Sayri",
            [text("Telegram bot connection", accent=True),
             text("\n".join(lines), dim=True),
             note("Talk to @BotFather to create your bot and get its 'Bot Token'.", "info")],
            id="home", footer=footer,
        )

    def _configure_screen(self) -> dict:
        return screen(
            "Configure Telegram bot",
            [
                note("1. Open @BotFather in Telegram → /newbot\n"
                     "2. Copy the token it gives you (format 123456:ABC-DEF...)\n"
                     "3. Paste it here and save.", "info"),
                entry("token", "Bot Token", default=self.val.get("token", ""),
                      secret=True, hint="Saved to ~/.config/sayri/secrets.json."),
            ],
            footer=[button("back", "Back"), button("save", "Save", kind="primary")],
            id="configure", step="Step 1/1",
        )

    def _pin_screen(self) -> dict:
        pin = get_pin() or rotate_pin()
        return screen(
            "Pairing PIN",
            [text(pin, accent=True),
             sub("Send it to your bot: /pair " + pin),
             note("The PIN rotates daily and after each pairing.", "info")],
            footer=[button("regenerate", "Generate another"), button("back", "Back"),
                    button("quit", "Close")],
            id="pin", step="Step 2/2",
        )

    def dispatch(self, event: dict) -> Optional[dict]:
        t = event.get("type")
        if t == "poll":
            return self.render()
        if t == "action":
            return self._on_action(event.get("widget", ""), event.get("value", {}))
        if t == "submit":
            return self._on_submit(event.get("value", {}))
        return self.render()

    def _on_action(self, wid: str, value: Optional[dict] = None) -> Optional[dict]:
        value = value or {}
        if wid == "configure":
            self.idx = 1
            return self.render()
        if wid == "pin":
            self.idx = 2
            return self.render()
        if wid == "save":
            return self._save(value)
        if wid == "regenerate":
            rotate_pin()
            return self.render()
        if wid == "back":
            self.idx = max(0, self.idx - 1)
            return self.render()
        if wid in ("quit", "close"):
            return None
        return self.render()

    def _on_submit(self, value: dict) -> Optional[dict]:
        return self._save(value) if self.idx == 1 else self.render()

    def _save(self, value: dict) -> dict:
        if value.get("token"):
            set_secret(str(value["token"]))
            self.val["token"] = str(value["token"])
        self.idx = 0
        return self._home_screen()


# ---------------------------------------------------------------- surfaces
def wizard_app() -> TelegramWizard:
    return TelegramWizard()


def run_tui(stream: Any = None, out: Any = None) -> int:
    """Terminal surface — interactive TUI or piped line mode. No CLI parsing."""
    return run_cli(TelegramWizard(), stream=stream, out=out)


def run_tui_guard() -> int:
    try:
        return run_tui()
    except KeyboardInterrupt:
        print("\n  cancelled.")
        return 130


def panel_page(wizard: Optional[TelegramWizard] = None, transport: Optional[dict] = None) -> str:
    wizard = wizard or wizard_app()
    transport = transport or {"kind": "fetch", "endpoint": "/api/event"}
    return wizard_page(wizard.render(), title=PANEL_TITLE, transport=transport)


# ------------------------------------------------------------ HTTP panel
def make_handler(wizard: TelegramWizard, status_fn: Optional[Callable[[], Any]] = None,
                 page_fn: Optional[Callable[[], str]] = None):
    class PanelHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quiet console
            pass

        def _json(self, payload: Any, code: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _html(self, html: str) -> None:
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/status" and status_fn:
                self._json(status_fn())
                return
            self._html(page_fn() if page_fn else panel_page(wizard))

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/event":
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, 400)
                return
            scr = wizard.dispatch(event)
            self._json(scr if scr is not None else {"title": "", "closed": True})

    return PanelHandler


def serve_http(host: str = "127.0.0.1", port: int = 8756,
               wizard: Optional[TelegramWizard] = None) -> None:
    """Local browser panel. Runs until interrupted (no CLI parsing here)."""
    wizard = wizard or wizard_app()
    handler = make_handler(wizard, status_fn=status_payload)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Telegram panel →  http://{host}:{port}")
    print("  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  panel stopped.")
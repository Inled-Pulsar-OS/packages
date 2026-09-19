#!/usr/bin/env python3
"""Sayri Orb UI — GTK4 transparent widget plugin for the Sayri daemon.

Connects to the daemon's ``sayri-daemon.sock`` over the NDJSON IPC protocol,
renders the Siri-style orb with state/level feedback, and sends commands on
interaction.  Self-contained — no ``import sayri.*`` required so it runs
standalone under the gateway supervisor.

States mapped to colours:
  idle     = dim green
  activated/listening = bright green + level ring
  thinking = pulsing yellow/orange
  speaking = pulsing blue
  error    = flash red
"""

from __future__ import annotations

import json
import math
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Minimal self-contained IPC client (no sayri.ipc dependency)
# ---------------------------------------------------------------------------

SOCK_NAME = "sayri-daemon.sock"
LEGACY_SOCK_NAME = "sayri.sock"


def _state_dir() -> str:
    return os.environ.get("SAYRI_STATE_DIR") or str(Path.home() / ".local" / "share" / "sayri")


def _find_socket() -> Optional[str]:
    state = Path(_state_dir())
    for name in (SOCK_NAME, LEGACY_SOCK_NAME):
        p = state / name
        if p.exists():
            return str(p)
    return None


class _IPC:
    """Minimal synchronous NDJSON IPC client."""

    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._next_id = 1
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._event_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[threading.Thread] = None

    def connect(self, timeout: float = 5.0) -> bool:
        if self._sock is not None:
            return True
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.sock_path)
            s.settimeout(None)
            self._sock = s
            self._start_reader()
            return True
        except (OSError, FileNotFoundError):
            return False

    def close(self) -> None:
        s = self._sock
        self._sock = None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def request(self, cmd: str, params: Optional[dict] = None, timeout: float = 30.0) -> Any:
        if not self.connect():
            raise ConnectionError("daemon not running")
        rid = self._next_id
        self._next_id += 1
        done = threading.Event()
        with self._lock:
            self._pending[rid] = done
        line = (json.dumps({"cmd": cmd, "params": params or {}, "id": rid}) + "\n").encode()
        self._sock.sendall(line)
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"{cmd} timed out")
        return self._results.pop(rid, None)

    def on_event(self, cb: Callable[[dict], None]) -> None:
        self._event_cb = cb

    def _start_reader(self) -> None:
        if self._reader and self._reader.is_alive():
            return
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while True:
            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if "event" in msg and self._event_cb:
                        try:
                            self._event_cb(msg)
                        except Exception:
                            pass
                    elif "id" in msg:
                        rid = msg.get("id")
                        with self._lock:
                            ev = self._pending.pop(rid, None)
                        if ev:
                            self._results[rid] = msg
                            ev.set()
            except OSError:
                break

    # convenience wrappers
    def ping(self) -> bool:
        try:
            return self.request("ping", timeout=3) == "pong"
        except Exception:
            return False

    def toggle_listening(self) -> None:
        self.request("toggle_listening")

    def interrupt(self) -> None:
        self.request("interrupt")

    def new_conversation(self) -> None:
        self.request("new_conversation")

    def status(self) -> dict:
        return self.request("status", timeout=5) or {}


# ---------------------------------------------------------------------------
# GTK4 Orb
# ---------------------------------------------------------------------------

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gsk", "4.0")
    from gi.repository import GLib, Gtk, Gdk, Gsk
    _GTK_OK = True
except Exception:
    _GTK_OK = False

# State → RGBA colours (R, G, B, A)
_COLOURS: dict[str, tuple[float, float, float, float]] = {
    "idle":       (0.20, 0.72, 0.35, 0.55),
    "activated":  (0.20, 0.85, 0.40, 0.90),
    "listening":  (0.20, 0.85, 0.40, 0.95),
    "thinking":   (0.90, 0.65, 0.15, 0.85),
    "speaking":   (0.25, 0.55, 0.95, 0.90),
    "error":      (0.95, 0.20, 0.20, 0.85),
}
_STATE_ORDER = ["idle", "activated", "listening", "thinking", "speaking", "error"]


class OrbWindow(Gtk.ApplicationWindow):
    """Transparent always-on-top circular orb with level meter."""

    def __init__(self, app: Gtk.Application, ipc: _IPC) -> None:
        super().__init__(application=app, title="Sayri")
        self.ipc = ipc
        self._state = "idle"
        self._level = 0.0
        self._target_level = 0.0
        self._partial = ""
        self._anim_phase = 0.0

        # Window setup: borderless, transparent, always on top
        self.set_decorated(False)
        self.set_default_size(140, 140)
        self.set_resizable(False)

        # CSS for transparency
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window { background: transparent; }
            orb-drawing { background: transparent; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Drawing area
        self._area = Gtk.DrawingArea()
        self._area.set_content_width(140)
        self._area.set_content_height(140)
        self._area.set_draw_func(self._draw)
        self.set_child(self._area)

        # Click → toggle listening
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self._on_click())
        self._area.add_controller(click)

        # Escape / Q → quit
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self._area.add_controller(key)

        # Animation timer (30 fps)
        GLib.timeout_add(33, self._tick)

    # ── drawing ──────────────────────────────────────────────────────
    def _draw(self, area: Gtk.DrawingArea, cr, w: int, h: int) -> None:
        cx, cy = w / 2, h / 2
        base_r = min(w, h) / 2 - 6
        cr.save()

        # Outer glow (level ring)
        if self._level > 0.01:
            ring_r = base_r + 4 + self._level * 10
            a = 0.25 + self._level * 0.35
            cr.set_source_rgba(0.25, 0.80, 0.45, a)
            cr.arc(cx, cy, ring_r, 0, 2 * math.pi)
            cr.fill()

        # Base orb
        r, g, b, a = _COLOURS.get(self._state, _COLOURS["idle"])

        # Pulse for thinking/speaking
        if self._state in ("thinking", "speaking"):
            pulse = 0.08 * math.sin(self._anim_phase * 4)
            a = max(0.0, min(1.0, a + pulse))
            r += pulse * 0.5
            g += pulse * 0.3

        cr.set_source_rgba(r, g, b, a)
        cr.arc(cx, cy, base_r, 0, 2 * math.pi)
        cr.fill()

        # Inner highlight
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.12)
        cr.arc(cx - base_r * 0.15, cy - base_r * 0.15, base_r * 0.55, 0, 2 * math.pi)
        cr.fill()

        cr.restore()

    # ── animation ────────────────────────────────────────────────────
    def _tick(self) -> bool:
        self._anim_phase += 0.05
        # Smooth level interpolation
        self._level += (self._target_level - self._level) * 0.3
        if abs(self._level - self._target_level) < 0.005:
            self._level = self._target_level
        self._area.queue_draw()
        return True  # keep timer alive

    # ── interaction ──────────────────────────────────────────────────
    def _on_click(self) -> None:
        try:
            self.ipc.toggle_listening()
        except Exception:
            pass

    def _on_key(self, _ctrl, keyval, _keycode, _mods) -> bool:
        if keyval == 0xFF1B:  # Escape
            try:
                self.ipc.interrupt()
            except Exception:
                pass
            return True
        if keyval in (0x71, 0x51):  # q / Q
            try:
                self.ipc.new_conversation()
            except Exception:
                pass
            return True
        return False

    # ── event handlers (called from IPC reader via GLib.idle_add) ────
    def _on_state(self, ev: dict) -> None:
        self._state = ev.get("state", "idle")

    def _on_audio_level(self, ev: dict) -> None:
        self._target_level = float(ev.get("level", 0.0))

    def _on_partial(self, ev: dict) -> None:
        self._partial = ev.get("text", "")

    def _on_error(self, ev: dict) -> None:
        self._state = "error"
        GLib.timeout_add(2000, self._clear_error)

    def _clear_error(self) -> bool:
        if self._state == "error":
            self._state = "idle"
        return False

    def _on_show(self, _ev: dict) -> None:
        self.present()

    def _on_hide(self, _ev: dict) -> None:
        self.set_visible(False)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class OrbApp(Gtk.Application):
    def __init__(self, ipc: _IPC) -> None:
        super().__init__(application_id="es.inled.sayri.orb")
        self.ipc = ipc
        self.window: Optional[OrbWindow] = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = OrbWindow(self, self.ipc)
            self.window.present()

        # Wire IPC events → GTK main thread
        _EVENT_MAP = {
            "state":           self.window._on_state,
            "audio_level":     self.window._on_audio_level,
            "partial":         self.window._on_partial,
            "error":           self.window._on_error,
            "show":            self.window._on_show,
            "hide":            self.window._on_hide,
            "assistant_delta": self.window._on_partial,
            "hint":            self.window._on_partial,
        }

        def _dispatch(ev: dict) -> None:
            name = ev.get("event", "")
            handler = _EVENT_MAP.get(name)
            if handler:
                GLib.idle_add(handler, ev)

        self.ipc.on_event(_dispatch)

        # Request initial state
        try:
            st = self.ipc.status()
            if st:
                GLib.idle_add(self.window._on_state, {"state": st.get("state", "idle")})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    sock = _find_socket()
    if not sock:
        print("[sayri-orb] daemon socket not found; is the daemon running?", file=sys.stderr)
        return 1

    ipc = _IPC(sock)
    if not ipc.connect():
        print("[sayri-orb] could not connect to daemon socket", file=sys.stderr)
        return 1

    print(f"[sayri-orb] connected to {sock}")

    if not _GTK_OK:
        print("[sayri-orb] GTK4 not available; running headless (CLI only)", file=sys.stderr)
        try:
            ipc.ping()
            print("[sayri-orb] daemon is alive")
        finally:
            ipc.close()
        return 0

    # Daemon PID file for lifecycle
    pid_file = Path(_state_dir()) / "orb.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))
    except Exception:
        pass

    def _cleanup(*_):
        try:
            ipc.close()
        except Exception:
            pass
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    app = OrbApp(ipc)
    try:
        app.run([])
    finally:
        _cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
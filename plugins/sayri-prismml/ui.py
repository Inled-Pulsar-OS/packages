#!/usr/bin/env python3
"""Prism ML · UI layer — cross-renderer xui host + HTTP panel.

Decoupled from the CLI: this module has no argparse and no command parsing.
It exposes the wizard host (render/dispatch), the terminal runner, the browser
page builder and the local HTTP server. The CLI lives in ``gateway.py`` and
the model/server logic in ``prismml.py``.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

try:
    import prismml
    import xui
except ImportError as exc:  # pragma: no cover - only when run outside the plugin dir
    sys.stderr.write(f"[prismml] plugin dependencies missing (run from the plugin dir): {exc}\n")
    raise


def _quant_opts(family: str) -> list:
    """Select options for a family using the real quant list + descriptions."""
    quants = prismml.QUANTS.get(family, prismml.QUANTS["ternary"])
    return [
        {"value": q, "label": q.upper(), "desc": prismml.QUANT_LABELS.get(q, q)}
        for q in quants
    ]


FAMILY_OPTS = [
    {"value": "ternary", "label": "Ternary-Bonsai", "desc": "quantized ternary mode (recommended, PQ2_0)"},
    {"value": "bonsai", "label": "Bonsai", "desc": "classic 1-bit GGUF quantization (Q1_0)"},
]

SIZE_OPTS = [
    {"value": "27B", "label": "27B", "desc": "maximum quality · needs a lot of RAM"},
    {"value": "8B", "label": "8B", "desc": "balanced (recommended)"},
    {"value": "4B", "label": "4B", "desc": "fast and light"},
    {"value": "1.7B", "label": "1.7B", "desc": "minimal, runs even on CPU"},
]

PANEL_TITLE = "Prism ML · Sayri"

STEPS_TOTAL = 4


def _quant_display(family: str, quant: str) -> str:
    q = prismml.effective_quant(family, quant or "", default=True)
    return prismml.QUANT_LABELS.get(q, q.upper())


class PrismWizard:
    """Wizard host — runs in the terminal (TUI), the browser panel and HTTP."""

    def __init__(self) -> None:
        self.cfg = prismml.Config()
        self.val: dict = {
            "family": self.cfg.get("family", "ternary"),
            "size": self.cfg.get("size", "8B"),
            "quant": self.cfg.get("quant", "") or "",
            "port": self.cfg.get("port", 8080),
            "download_bin": True,
            "download_model": True,
            "start_after": True,
        }
        self.idx = 0
        self.task = xui.TaskRunner()
        self._on_task_done: Optional[Callable[[Any], None]] = None
        self._final: Optional[dict] = None

    # ------------------------------------------------------------ screens
    def _step_label(self, n: int) -> str:
        return f"Step {n}/{STEPS_TOTAL}"

    def _status(self) -> dict:
        return prismml.Server(self.cfg).status_payload()

    def _status_screen(self, done: bool = False) -> dict:
        st = self._status()
        body: list = [
            xui.text(f"Family {st['family']} · {st['size']} · {st['quant'].upper()}", accent=True),
            xui.sub(f"GPU: {st['gpu']}  ·  port {st['port']}"),
        ]
        msgs = []
        msgs.append(f"model: {'✓ downloaded' if st['model'] else '✗ pending'}")
        msgs.append(f"llama-server binary: {'✓ installed' if st['binary'] else '✗ pending'}")
        if st["running"]:
            msgs.append(f"server: running (PID {st['pid']}) — health: {'ok' if st['health'] else 'starting'}")
        else:
            msgs.append("server: stopped")
        msgs.append(f"Endpoint: {st['endpoint']}")
        body.append(xui.text("\n".join(msgs), dim=True))
        body.append(xui.note("100% local model: no API key, no cloud.", "info"))
        footer = [xui.button("open", "Open chat", kind="primary"), xui.button("quit", "Close")]
        return xui.screen(
            "Prism ML · Sayri", body, id="status", done=done, footer=footer,
        )

    def render(self) -> Optional[dict]:
        if self.idx == 0:
            return self._home_screen()
        if self.idx == 1:
            return self._family_screen()
        if self.idx == 2:
            return self._quant_screen()
        if self.idx == 3:
            return self._size_screen()
        if self.idx == 4:
            return self._runtime_screen()
        return self._final or self._status_screen(done=True)

    def _home_screen(self) -> dict:
        body = [
            xui.text("Welcome to the Prism ML server", accent=True),
            xui.sub("Runs a Bonsai model fully locally."),
        ]
        body += [xui.text(line, dim=True) for line in self._status_body_lines()]
        body.append(xui.note(f"Detected GPU: {prismml.detect_gpu()}", "info"))
        footer = [
            xui.button("run", "Install & run", kind="primary"),
            xui.button("conf", "Configure"),
            xui.button("quit", "Close", kind="secondary"),
        ]
        return xui.screen("Prism ML · Sayri", body, id="home", footer=footer)

    def _status_body_lines(self) -> list:
        st = self._status()
        return [
            f"model {st['family']}/{st['size']} ({st['quant'].upper()}): {'✓' if st['model'] else '✗'}  binary: {'✓' if st['binary'] else '✗'}",
            f"server: {'running (PID %s)' % st['pid'] if st['running'] else 'stopped'} · {st['endpoint']}",
        ]

    def _detect_body(self) -> dict:
        """Real download state for the current selection (family/size/quant)."""
        family = self.val.get("family", "ternary")
        size = self.val.get("size", "8B")
        quant = self.val.get("quant", "") or ""
        binary_ok = prismml.llama_server_bin().is_file()
        model_ok = prismml.model_file(family, size, quant).is_file()
        lines = []
        lines.append(f"llama-server binary: {'✓ already downloaded' if binary_ok else '✗ not downloaded'}")
        lines.append(f"model ({size}, {quant.upper() or 'auto'}): {'✓ already downloaded' if model_ok else '✗ not downloaded'}")
        return {"binary": binary_ok, "model": model_ok, "lines": lines}

    def _family_screen(self) -> dict:
        body = [xui.select("family", "Model family", FAMILY_OPTS, self.val.get("family", "ternary"))]
        body.append(xui.note(
            "On the next step you can choose quantization and size. "
            "Anything already downloaded is detected and reused.", "info"))
        return xui.screen(
            "Choose the family",
            body,
            footer=[xui.button("back", "Back"), xui.button("next", "Next", kind="primary")],
            id="family", step=self._step_label(1),
        )

    def _quant_screen(self) -> dict:
        family = self.val.get("family", "ternary")
        opts = _quant_opts(family)
        low = self.val.get("quant", "") or ""
        if low not in prismml.QUANTS.get(family, []):
            low = prismml.DEFAULT_QUANT.get(family, "pq2_0")
        body = [
            xui.select("quant", "Quantization", opts, low),
        ]
        if family == "bonsai":
            body.append(xui.note("Bonsai is a 1-bit model: Q1_0 is the only option.", "info"))
        else:
            body.append(xui.note(
                "PQ2_0 is the recommended one (lightest). F16 gives the best quality but is several "
                "times larger. The choice decides which .gguf file gets downloaded.", "info"))
        det = self._detect_body()
        body.append(xui.text("\n".join(det["lines"]), dim=True))
        return xui.screen(
            "Quantization",
            body,
            footer=[xui.button("back", "Back"), xui.button("next", "Next", kind="primary")],
            id="quant", step=self._step_label(2),
        )

    def _size_screen(self) -> dict:
        family = self.val.get("family", "ternary")
        quant = self.val.get("quant", "") or ""
        body = [xui.select("size", "Size", SIZE_OPTS, self.val.get("size", "8B"))]
        det = self._detect_body()
        if det["model"]:
            body.append(xui.note(f"✓ The {self.val.get('size', '8B')} ({quant.upper() or 'auto'}) model is already downloaded — it will be reused.", "info"))
        else:
            body.append(xui.text("\n".join(det["lines"]), dim=True))
        return xui.screen(
            "Model size",
            body,
            footer=[xui.button("back", "Back"), xui.button("next", "Next", kind="primary")],
            id="size", step=self._step_label(3),
        )

    def _runtime_screen(self) -> dict:
        binary_ok = prismml.llama_server_bin().is_file()
        family = self.val.get("family", "ternary")
        quant = _quant_display(family, self.val.get("quant", "") or "")
        body = [
            xui.note(f"Detected GPU: {prismml.detect_gpu()} — platform {prismml.platform_asset()}", "info"),
            xui.entry("port", "Port", default=str(self.val.get("port", 8080)),
                      placeholder="8080", hint="Endpoint /health at http://127.0.0.1:<port>"),
        ]
        det = self._detect_body()
        if det["binary"] and det["model"]:
            body.append(xui.note("✓ Everything is already downloaded: 'Install & run' will reuse it without downloading again.", "info"))
        else:
            body.append(xui.text("\n".join(det["lines"]), dim=True))
            body.append(xui.note("You'll see ✓ marks for what you already have: it continues from there.", "info"))
        body += [
            xui.check("download_bin", "Download llama-server binary (~100 MB)", default=bool(self.val.get("download_bin", not binary_ok))),
            xui.check("download_model", f"Download the GGUF model ({quant})", default=bool(self.val.get("download_model", True))),
            xui.check("start_after", "Start the server when done", default=bool(self.val.get("start_after", True))),
        ]
        return xui.screen(
            "Runtime and downloads",
            body,
            footer=[xui.button("back", "Back"), xui.button("run", "Install & run", kind="primary")],
            id="runtime", step=self._step_label(STEPS_TOTAL),
        )

    # ------------------------------------------------------------ events
    def dispatch(self, event: dict) -> Optional[dict]:
        t = event.get("type")
        if self.task.running:
            if t == "poll":
                return self._poll_handle()
            return self._progress_screen()
        if t == "poll":
            return self.render()
        if t == "action":
            return self._on_action(event.get("widget", ""), event.get("value", {}))
        if t == "submit":
            return self._on_submit(event.get("value", {}))
        return self.render()

    def _poll_handle(self) -> dict:
        if self.task.running:
            return self._progress_screen()
        info = self.task.poll()
        if info["error"]:
            self._final = self._error_screen(info["error"])
            return self._final
        cb = self._on_task_done
        if cb:
            self._on_task_done = None
            try:
                cb(info["result"])
            except Exception as exc:  # noqa: BLE001
                self._final = self._error_screen(str(exc))
                return self._final
        return self.render()

    def _progress_screen(self) -> dict:
        info = self.task.poll()
        body: list = [xui.text("One moment…", accent=True)]
        lines = list(info["log"][-6:])
        if info["running"]:
            body.append(xui.progress(info["log"][-1] if info["log"] else "Downloading…", info["progress"]))
            if lines:
                body.append(xui.text("\n".join(lines), dim=True))
            body.append(xui.sub("We will continue automatically when it finishes."))
            return xui.screen("Working…", body, footer=[], id="busy", busy=True)
        return xui.screen("Working…", body, footer=[], id="idle", busy=False)

    def _error_screen(self, msg: str) -> dict:
        return xui.screen(
            "Something went wrong",
            [xui.note(msg, "error"), xui.note("Check network/disk and retry from 'Configure'.", "info")],
            footer=[xui.button("retry", "Retry"), xui.button("quit", "Close")],
            id="error", done=True,
        )

    def _on_action(self, wid: str, value: Optional[dict] = None) -> Optional[dict]:
        value = value or {}
        if wid == "run":
            self._apply(value)
            self._launch_task("Downloading and starting…", self._task_all, self._finalize)
            return self._progress_screen()
        if wid == "conf":
            self.idx = 1
            return self.render()
        if wid == "back":
            self.idx = max(0, self.idx - 1)
            return self.render()
        if wid in ("next", "siguiente"):
            return self._on_submit(value)
        if wid == "retry":
            self.idx = 4
            return self.render()
        if wid == "open":
            try:
                webbrowser.open(prismml.Server(self.cfg).endpoint())
            except Exception:  # noqa: BLE001
                pass
            return None
        if wid in ("quit", "close", "cerrar"):
            return None
        return self.render()

    def _on_submit(self, value: dict) -> Optional[dict]:
        if self.idx == 1:
            if value.get("family"):
                family = str(value["family"])
                self.val["family"] = family
                if self.val.get("quant", "") not in prismml.QUANTS.get(family, []):
                    self.val["quant"] = prismml.DEFAULT_QUANT.get(family, "pq2_0")
            self.idx = 2
            return self.render()
        if self.idx == 2:
            if value.get("quant"):
                self.val["quant"] = str(value["quant"])
            self.idx = 3
            return self.render()
        if self.idx == 3:
            if value.get("size"):
                self.val["size"] = str(value["size"])
            self.idx = 4
            return self.render()
        return self.render()

    def _apply(self, value: dict) -> None:
        for key in ("family", "size"):
            if value.get(key):
                self.val[key] = str(value[key])
        if value.get("quant"):
            self.val["quant"] = str(value["quant"])
        port = value.get("port")
        if port:
            try:
                self.val["port"] = int(str(port).strip())
            except (TypeError, ValueError):
                pass
        for key in ("download_bin", "download_model", "start_after"):
            if key in value:
                self.val[key] = bool(value[key])
        self.cfg.merge({
            "family": self.val.get("family"),
            "size": self.val.get("size"),
            "quant": self.val.get("quant", ""),
            "port": self.val.get("port"),
        })
        self.cfg.save()

    def _finalize(self, _result: Any = None) -> None:
        self.idx = 99

    def _launch_task(self, label: str, fn: Callable, cb: Optional[Callable]) -> None:
        self._on_task_done = cb
        self.task.start(label, fn)

    def _task_all(self, progress: Callable, log: Callable) -> Any:
        family = self.cfg.get("family", "ternary")
        size = self.cfg.get("size", "8B")
        quant = self.cfg.get("quant", "") or ""
        is_win = platform.system().lower() == "windows"
        binary_missing = not (prismml.llama_server_bin().is_file() and (is_win or os.access(prismml.llama_server_bin(), os.X_OK)))
        model_missing = not prismml.model_file(family, size, quant).is_file()
        if self.val.get("download_bin", True) or binary_missing:
            prismml.install_binary(progress=progress, log=log)
        else:
            log(["llama-server binary already installed ✓"])
        if self.val.get("download_model", True) or model_missing:
            prismml.install_model(family, size, quant, progress=progress, log=log)
        else:
            log(["Model already downloaded ✓"])
        if self.val.get("start_after", True):
            srv = prismml.Server(self.cfg)
            srv.start(log=log)
            return srv
        return None


# ---------------------------------------------------------------- surfaces
def wizard_app() -> PrismWizard:
    return PrismWizard()


def run_tui(stream: Any = None, out: Any = None) -> int:
    """Terminal surface — interactive TUI or piped line mode. No CLI parsing."""
    return xui.run_cli(PrismWizard(), stream=stream, out=out)


def run_tui_guard() -> int:
    try:
        return run_tui()
    except KeyboardInterrupt:
        print("\n  cancelled.")
        return 130


def panel_page(wizard: Optional[PrismWizard] = None, transport: Optional[dict] = None) -> str:
    """Self-contained HTML page for the panel (fetch transport by default)."""
    wizard = wizard or wizard_app()
    transport = transport or {"kind": "fetch", "endpoint": "/api/event"}
    return xui.wizard_page(wizard.render(), title=PANEL_TITLE, transport=transport)


# ------------------------------------------------------------ HTTP panel
def make_handler(wizard: PrismWizard, status_fn: Optional[Callable[[], Any]] = None,
                 page_fn: Optional[Callable[[], str]] = None):
    """Build a BaseHTTPRequestHandler bound to a wizard instance and status."""

    class PanelHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
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
               wizard: Optional[PrismWizard] = None) -> None:
    """Local browser panel. Runs until interrupted (no CLI parsing here)."""
    wizard = wizard or wizard_app()
    handler = make_handler(wizard, status_fn=lambda: prismml.Server(wizard.cfg).status_payload())
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Prism ML panel →  http://{host}:{port}")
    print("  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  panel stopped.")
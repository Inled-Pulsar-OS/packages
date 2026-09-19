#!/usr/bin/env python3
"""sayri-ui-desktop — gateway for the Sayri desktop UI.

Thin launcher: the actual UI is the built-in GTK4/WebKit app, reached through
`python3 -m sayri --launch-ui`. This plugin exists so the UI is a first-class
Sayri plugin (`sayri ui default` finds it by id "sayri-desktop") while the
underlying process still boots the normal SAYRI GUI.

The args mirror the CLI: start (default), stop, status, open.
"""

import os
import signal
import subprocess
import sys


def _pid_file() -> str:
    return os.path.join(
        os.environ.get("SAYRI_STATE_DIR")
        or os.path.expanduser("~/.local/share/sayri"),
        "ui-desktop.pid",
    )


def _status() -> int:
    pid_file = _pid_file()
    if os.path.isfile(pid_file):
        try:
            pid = int(open(pid_file, encoding="utf-8").read().strip())
            os.kill(pid, 0)
            print("sayri-desktop: running")
            return 0
        except (OSError, ValueError):
            pass
    print("sayri-desktop: stopped")
    return 0


def _start() -> int:
    cmd = [sys.executable, "-m", "sayri", "--launch-ui"]
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(_pid_file(), "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    print(f"sayri-desktop: started (pid {proc.pid})")
    return 0


def _stop() -> int:
    pid_file = _pid_file()
    if os.path.isfile(pid_file):
        try:
            pid = int(open(pid_file, encoding="utf-8").read().strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
    try:
        os.unlink(pid_file)
    except OSError:
        pass
    print("sayri-desktop: stop requested")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if a in ("start", "stop", "status", "open")]
    action = args[0] if args else "start"
    if action == "stop":
        return _stop()
    if action == "status":
        return _status()
    return _start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
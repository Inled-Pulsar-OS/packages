#!/usr/bin/env python3
"""Conversation memory journal for the Discord gateway.

Appends the messages that matter — tasks, proposals and questions — from
authorized members to per-channel Markdown journals::

    ~/.config/sayri/discord_memory/<instance_id>/channel_<channel_id>.md

This module is deliberately local and passive:

- It **never** replies on Discord and **never** talks to Sayri Core; it only
  appends text to a file. Who is allowed to be journaled is decided by the
  gateway (``AuthorizationManager.can_interact``), so strangers and channels
  with guests disabled are never captured.
- ``channel_id`` is reduced to digits before it becomes part of a filename, so
  a crafted identifier cannot escape the memory directory (no path traversal).
- Captured text is stripped of control characters and truncated, so a message
  cannot inject terminal escapes or bloat a journal without bounds.
- The directory is created 0700 and journals 0600, because they hold the
  owner's conversation excerpts.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Heuristic markers keep this dependency-free, predictable and cheap: the
# gateway classifies every visible message, so it must never block.
MARKERS: Dict[str, Tuple[str, ...]] = {
    "task": ("tarea", "task", "to-do", "todo", "hacer", "make", "create",
             "run", "execute", "instalar", "install", "fix", "arreglar",
             "deploy", "build", "review"),
    "proposal": ("propuesta", "proposal", "propongo", "sugerencia", "suggestion",
                 "idea", "what if", "how about", "y si"),
    "question": ("?", "how", "what", "why", "when", "where", "who", "como",
                 "cómo", "qué", "por que", "por qué", "cuándo", "dónde"),
}

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_TEXT_CHARS = 4000
MAX_AUTHOR_CHARS = 80
MAX_CHANNEL_DIGITS = 32


def classify(text: str) -> Optional[str]:
    """Return 'task', 'proposal', 'question' or None for a message body."""
    lowered = (text or "").lower().strip()
    if not lowered:
        return None
    for kind in ("task", "proposal", "question"):
        if any(marker in lowered for marker in MARKERS[kind]):
            return kind
    return None


def safe_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Strip control characters and cap the length of captured text."""
    return CONTROL_CHARS.sub("", text or "").strip()[:limit]


def channel_file(directory: Path, channel_id: object) -> Path:
    """Journal path for a channel; the id is digits-only (no traversal)."""
    digits = re.sub(r"[^0-9]", "", str(channel_id))[:MAX_CHANNEL_DIGITS] or "unknown"
    return Path(directory) / f"channel_{digits}.md"


def _safe_instance(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(instance_id))[:64] or "default"


class MemoryStore:
    """Append-only per-channel Markdown journal.

    The gateway owns the *enabled* decision (it lives in the authorization
    file, together with guest access, and is toggled with ``/memory``), so this
    store stays a dumb, auditable writer.
    """

    def __init__(self, base_dir: Path, instance_id: str) -> None:
        self.directory = Path(base_dir) / _safe_instance(instance_id)

    # ── writing ─────────────────────────────────────────────────────

    def capture(self, channel_id: object, author: str, text: str) -> Optional[str]:
        """Classify ``text`` and append it. Returns the kind, or None."""
        kind = classify(text)
        body = safe_text(text)
        if not kind or not body:
            return None
        self._ensure_dir()
        path = channel_file(self.directory, channel_id)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        record = f"\n## [{stamp}] {kind} by {safe_text(author, MAX_AUTHOR_CHARS)}\n{body}\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, record.encode("utf-8", "replace"))
        finally:
            os.close(fd)
        return kind

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    # ── reading (used by the UI and the /memory command) ────────────

    def entries(self, channel_id: object, limit: int = 5) -> List[str]:
        """Last ``limit`` records of a channel journal (headers + bodies)."""
        path = channel_file(self.directory, channel_id)
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        blocks = [b.strip() for b in raw.split("\n## ") if b.strip()]
        return ["## " + b if not b.startswith("[") else b for b in blocks][-limit:]

    def stats(self) -> Dict[str, object]:
        """Journal summary for the status screens: files, entries, last write."""
        files = 0
        total_bytes = 0
        last = 0.0
        if self.directory.is_dir():
            for item in self.directory.glob("channel_*.md"):
                try:
                    info = item.stat()
                except OSError:
                    continue
                files += 1
                total_bytes += info.st_size
                last = max(last, info.st_mtime)
        return {
            "directory": str(self.directory),
            "files": files,
            "bytes": total_bytes,
            "last_write": last,
        }

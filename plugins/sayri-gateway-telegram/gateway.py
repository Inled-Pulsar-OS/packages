#!/usr/bin/env python3
"""Sayri Telegram Gateway Daemon.

Connects to the Telegram Bot API via HTTPS Long-Polling, authenticates users
via Desktop OTP Pairing / Whitelist, and forwards messages to Sayri over local UNIX socket.
"""

import json
import os
import random
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

INSTANCE_ID = os.environ.get("SAYRI_GATEWAY_INSTANCE_ID", "default")
TARGET_AGENT = os.environ.get("SAYRI_TARGET_AGENT", "default")
SANDBOX_LEVEL = os.environ.get("SAYRI_SANDBOX_LEVEL", "")

AUTH_FILE = Path(os.environ.get("SAYRI_AUTH_FILE", str(Path.home() / ".config" / "sayri" / f"authorizations_{INSTANCE_ID}.json")))
SHARED_PIN_FILE = Path(os.environ.get("SAYRI_PIN_FILE", str(Path.home() / ".config" / "sayri" / f"pairing_pin_{INSTANCE_ID}.json")))
PID_FILE = Path(os.environ.get("SAYRI_PID_FILE", str(Path.home() / ".config" / "sayri" / f"gateway_{INSTANCE_ID}.pid")))
SOCKET_PATHS = [
    Path.home() / ".local" / "share" / "sayri" / "sayri.sock",
    Path(f"/run/user/{os.getuid()}/sayri/ipc.sock"),
    Path(f"/run/user/{os.getuid()}/sayri.sock"),
]


def acquire_single_instance_lock() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.is_file():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                try:
                    os.kill(old_pid, signal.SIGKILL)
                    time.sleep(0.3)
                except OSError:
                    pass
        except Exception:
            pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def markdown_to_telegram_html(text: str) -> str:
    """Converts standard Markdown into Telegram Bot API valid HTML."""
    if not text:
        return ""
    import html
    import re

    # 1. Protect code blocks
    code_blocks = []
    def _save_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        escaped_code = html.escape(code)
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{escaped_code}</code></pre>'
        else:
            tag = f'<pre>{escaped_code}</pre>'
        code_blocks.append(tag)
        return f"@@SAYRI_CODE_BLOCK_{idx}@@"

    res = re.sub(r"```([a-zA-Z0-9_\-]+)?\n?(.*?)\n?```", _save_block, text, flags=re.DOTALL)

    # 2. Protect inline code
    inline_codes = []
    def _save_inline(m):
        code = m.group(1)
        idx = len(inline_codes)
        tag = f'<code>{html.escape(code)}</code>'
        inline_codes.append(tag)
        return f"@@SAYRI_INLINE_{idx}@@"

    res = re.sub(r"`([^`\n]+)`", _save_inline, res)

    # 3. Escape general HTML entities in text
    res = html.escape(res, quote=False)

    # 4. Convert markdown links: [text](url) -> <a href="url">text</a>
    res = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<a href="\2">\1</a>', res)

    # 5. Bold: **text** or __text__
    res = re.sub(r'\*\*([^\*\n]+?)\*\*', r'<b>\1</b>', res)
    res = re.sub(r'__([^\_\n]+?)__', r'<b>\1</b>', res)

    # 6. Italic: *text* or _text_
    res = re.sub(r'(?<!\w)\*([^\*\n]+?)\*(?!\w)', r'<i>\1</i>', res)
    res = re.sub(r'(?<!\w)_([^\_\n]+?)_(?!\w)', r'<i>\1</i>', res)

    # 7. Strikethrough: ~~text~~
    res = re.sub(r'~~([^~\n]+?)~~', r'<s>\1</s>', res)

    # 8. Blockquotes: > line
    def _sub_quote(m):
        return f"<blockquote>{m.group(1).strip()}</blockquote>"
    res = re.sub(r'^(?:&gt;|>)\s*(.+)$', _sub_quote, res, flags=re.MULTILINE)

    # 9. Headers: # Header -> <b>Header</b>
    res = re.sub(r'^(?:#{1,6})\s+(.+)$', r'<b>\1</b>', res, flags=re.MULTILINE)

    # 10. Restore code blocks & inline code
    for i, block in enumerate(code_blocks):
        res = res.replace(f"@@SAYRI_CODE_BLOCK_{i}@@", block)
    for i, inline in enumerate(inline_codes):
        res = res.replace(f"@@SAYRI_INLINE_{i}@@", inline)

    return res


class TelegramClient:
    """Lightweight zero-dependency Telegram Bot API client."""

    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _api_call(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/{endpoint}"
        req = urllib.request.Request(url)
        if data:
            req.add_header("Content-Type", "application/json")
            req_data = json.dumps(data).encode("utf-8")
        else:
            req_data = None

        try:
            with urllib.request.urlopen(req, data=req_data, timeout=35) as res:
                body = res.read().decode("utf-8")
                return json.loads(body)
        except Exception as e:
            print(f"[Telegram] API error on {endpoint}: {e}", file=sys.stderr)
            return None

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> List[Dict[str, Any]]:
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        res = self._api_call("getUpdates", payload)
        if res and res.get("ok"):
            return res.get("result", [])
        return []

    def send_message(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None, parse_mode: Optional[str] = "HTML") -> Optional[Dict[str, Any]]:
        formatted = markdown_to_telegram_html(text) if parse_mode == "HTML" else text
        payload = {
            "chat_id": chat_id,
            "text": formatted[:4000] if len(formatted) > 4000 else formatted,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        res = self._api_call("sendMessage", payload)
        # Fallback to plain text if HTML rendering is rejected by Telegram API
        if (not res or not res.get("ok")) and parse_mode:
            payload.pop("parse_mode", None)
            payload["text"] = text[:4000] if len(text) > 4000 else text
            res = self._api_call("sendMessage", payload)
        return res

    def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode: Optional[str] = "HTML") -> Optional[Dict[str, Any]]:
        formatted = markdown_to_telegram_html(text) if parse_mode == "HTML" else text
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": formatted[:4000] if len(formatted) > 4000 else formatted,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        res = self._api_call("editMessageText", payload)
        # Fallback to plain text if HTML rendering is rejected by Telegram API
        if (not res or not res.get("ok")) and parse_mode:
            payload.pop("parse_mode", None)
            payload["text"] = text[:4000] if len(text) > 4000 else text
            res = self._api_call("editMessageText", payload)
        return res

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self._api_call("sendChatAction", {"chat_id": chat_id, "action": action})


class AuthorizationManager:
    """Enforces Desktop OTP Pairing and Whitelists with Brute-Force Rate Limiting."""

    def __init__(self):
        self.auth_file = AUTH_FILE
        self.whitelisted_users: Set[str] = set()
        self._failed_attempts: Dict[str, Tuple[int, float]] = {}  # user_id -> (count, last_attempt_time)
        self._load()

    def _load(self) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        if self.auth_file.is_file():
            try:
                data = json.loads(self.auth_file.read_text(encoding="utf-8"))
                self.whitelisted_users = set(str(u).lower() for u in data.get("allowed_telegram_users", []))
            except Exception:
                self.whitelisted_users = set()

    def _save(self) -> None:
        try:
            self.auth_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": time.time(),
                "allowed_telegram_users": list(self.whitelisted_users),
            }
            self.auth_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            try:
                os.chmod(self.auth_file, 0o600)
            except OSError:
                pass
        except Exception as e:
            print(f"[Auth] Save error: {e}", file=sys.stderr)

    def is_authorized(self, user_id: str, username: Optional[str] = None) -> bool:
        self._load()
        if str(user_id).lower() in self.whitelisted_users:
            return True
        if username and f"@{username.lower()}" in self.whitelisted_users:
            return True
        if username and username.lower() in self.whitelisted_users:
            return True
        return False

    def verify_pairing_pin(self, pin: str, user_id: str, username: Optional[str] = None) -> Tuple[bool, str]:
        import hmac

        # 1. Rate Limiting Check: Max 5 failed attempts per user ID in 10 minutes
        now = time.time()
        fail_count, last_fail = self._failed_attempts.get(str(user_id), (0, 0.0))
        if now - last_fail > 600:
            fail_count = 0  # reset cooldown
        if fail_count >= 5:
            cooldown_left = int(600 - (now - last_fail))
            print(f"[Auth Security] 🚨 Rate limit exceeded for user {user_id} ({cooldown_left}s remaining)")
            return False, f"Too many failed attempts. Please wait {cooldown_left} seconds before trying again."

        clean_pin = pin.replace(" ", "").replace("-", "").strip()
        print(f"[Auth] 🔍 Verifying candidate PIN for user @{username} (ID: {user_id})...")
        if not clean_pin:
            return False, "PIN code is empty."

        # Check desktop shared PIN file (~/.config/sayri/pairing_pin.json)
        if SHARED_PIN_FILE.is_file():
            try:
                data = json.loads(SHARED_PIN_FILE.read_text(encoding="utf-8"))
                file_pin = str(data.get("pin", "")).replace(" ", "").replace("-", "").strip()
                expires_at = data.get("expires_at", float("inf"))

                # Constant-time comparison to prevent timing attacks
                is_valid = hmac.compare_digest(clean_pin, file_pin) and time.time() <= expires_at
                if is_valid:
                    self.whitelisted_users.add(str(user_id).lower())
                    if username:
                        self.whitelisted_users.add(f"@{username.lower()}")
                    self._save()
                    self._failed_attempts.pop(str(user_id), None)
                    print(f"✅ [Sayri Auth] Successfully paired Telegram user @{username or user_id} ({user_id})!")

                    # Rotate desktop PIN after successful pairing
                    try:
                        import random
                        new_pin = f"{random.randint(100000, 999999)}"
                        SHARED_PIN_FILE.write_text(json.dumps({
                            "pin": new_pin,
                            "created_at": time.time(),
                            "expires_at": time.time() + 86400,
                        }, indent=2), encoding="utf-8")
                        os.chmod(SHARED_PIN_FILE, 0o600)
                    except Exception:
                        pass

                    return True, "Pairing completed successfully! Your account has been authorized in Sayri."
                else:
                    self._failed_attempts[str(user_id)] = (fail_count + 1, now)
                    remaining = 5 - (fail_count + 1)
                    print(f"[Auth] ❌ PIN mismatch or expired for user {user_id}. Remaining attempts: {remaining}")
                    return False, f"Incorrect or expired PIN. Remaining attempts: {max(0, remaining)}"
            except Exception as e:
                print(f"[Auth] Error checking shared pin file: {e}", file=sys.stderr)
        else:
            print(f"[Auth] ⚠️ PIN file not found at {SHARED_PIN_FILE}")

        self._failed_attempts[str(user_id)] = (fail_count + 1, now)
        return False, "No active pairing PIN found on desktop."


class SayriTelegramGateway:
    """Main Gateway loop with Continuous Sessions, Standby Inactivity Timeout, and History Recall."""

    def __init__(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print("❌ Error: TELEGRAM_BOT_TOKEN environment variable is not set.", file=sys.stderr)
            sys.exit(1)

        acquire_single_instance_lock()
        self.bot = TelegramClient(token)
        self.auth = AuthorizationManager()
        self.last_update_id: Optional[int] = None
        self._running = True

        # Inactivity timeout (seconds) and resume permission from Gateway Supervisor configuration
        self.allow_resume_previous = os.environ.get("SAYRI_ALLOW_RESUME_PREVIOUS", "1") == "1"
        try:
            self.inactivity_timeout = float(os.environ.get("SAYRI_INACTIVITY_TIMEOUT", "1800"))
        except ValueError:
            self.inactivity_timeout = 1800.0  # Default 30 minutes

        # Active session registry: chat_id -> {"session_id": str, "last_activity": float, "prev_session_id": Optional[str]}
        self._sessions: Dict[int, Dict[str, Any]] = {}

        # Ensure a valid desktop PIN file exists
        if not SHARED_PIN_FILE.is_file():
            import random
            raw_pin = f"{random.randint(100000, 999999)}"
            try:
                SHARED_PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
                SHARED_PIN_FILE.write_text(json.dumps({
                    "pin": raw_pin,
                    "created_at": time.time(),
                    "expires_at": time.time() + 86400
                }, indent=2), encoding="utf-8")
                print(f"[Gateway] Initialized desktop pairing PIN: {raw_pin}")
            except Exception as e:
                print(f"[Gateway] Error creating initial pin file: {e}")

    def find_sayri_socket(self) -> Optional[Path]:
        for p in SOCKET_PATHS:
            if p.exists():
                return p
        return None

    def _get_or_create_session(self, chat_id: int, user_name: str) -> Tuple[str, bool]:
        """Resolves active continuous session for chat, creating a new one if standby timeout elapsed."""
        now = time.time()
        session_info = self._sessions.get(chat_id)

        if session_info:
            last_activity = session_info.get("last_activity", 0.0)
            if now - last_activity > self.inactivity_timeout:
                # Standby timeout reached: archive previous session and spawn new clean one
                prev_id = session_info.get("session_id")
                new_session_id = f"tg-{INSTANCE_ID}-{chat_id}-{int(now)}"
                self._sessions[chat_id] = {
                    "session_id": new_session_id,
                    "last_activity": now,
                    "prev_session_id": prev_id,
                }
                print(f"[Gateway] ⏱️ Inactivity timeout reached for chat {chat_id}. Started new session: {new_session_id}")
                return new_session_id, True
            else:
                # Continuous conversation within timeout window
                session_info["last_activity"] = now
                return session_info["session_id"], False

        # First session for this chat
        new_session_id = f"tg-{INSTANCE_ID}-{chat_id}-{int(now)}"
        self._sessions[chat_id] = {
            "session_id": new_session_id,
            "last_activity": now,
            "prev_session_id": None,
        }
        return new_session_id, True

    def query_sayri_core_stream(
        self,
        prompt: str,
        user_name: str,
        session_id: str,
        chat_id: int,
        reply_to_msg_id: Optional[int] = None,
    ) -> None:
        """Sends query to Sayri IPC socket and streams live tool execution & deltas directly to Telegram."""
        # 1. Send initial thinking message
        sent_msg = self.bot.send_message(
            chat_id, "💭 *Thinking...*", reply_to_message_id=reply_to_msg_id
        )
        msg_id = (
            sent_msg.get("result", {}).get("message_id")
            if sent_msg and sent_msg.get("ok")
            else None
        )

        sock_path = self.find_sayri_socket()
        if not sock_path:
            fallback = f"👋 Hello {user_name}! Sayri received your message: '{prompt}'."
            if msg_id:
                self.bot.edit_message_text(chat_id, msg_id, fallback)
            else:
                self.bot.send_message(chat_id, fallback, reply_to_message_id=reply_to_msg_id)
            return

        status_prefix = ""
        current_text = ""
        last_edit_time = [time.time()]
        last_sent_text = ["💭 *Thinking...*"]

        def _update_ui(force: bool = False) -> None:
            now = time.time()
            if not force and (now - last_edit_time[0] < 0.8):
                return
            full_display = (status_prefix + current_text).strip()
            if not full_display:
                full_display = "💭 *Thinking...*"
            if full_display != last_sent_text[0] and msg_id:
                self.bot.edit_message_text(chat_id, msg_id, full_display)
                last_sent_text[0] = full_display
                last_edit_time[0] = now

        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(120.0)
            client.connect(str(sock_path))
            payload = {
                "type": "INCOMING_MSG",
                "author": user_name,
                "text": prompt,
                "target_agent": TARGET_AGENT,
                "sandbox_level": SANDBOX_LEVEL,
                "instance_id": INSTANCE_ID,
                "session_id": session_id,
            }
            client.sendall((json.dumps(payload) + "\n").encode("utf-8"))

            buffer = ""
            while True:
                data = client.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("{"):
                        try:
                            ev = json.loads(line)
                            event_name = ev.get("event")
                            if event_name == "tool_start":
                                cmd = ev.get("command", "")
                                status_prefix = f"⚙️ *Executing:* `{cmd[:60]}`…\n\n"
                                _update_ui(force=True)
                            elif event_name == "tool_finish":
                                cmd = ev.get("command", "")
                                code = ev.get("exit_code", 0)
                                if code == 0:
                                    status_prefix = f"⚙️ *Executed:* `{cmd[:60]}`\n\n"
                                else:
                                    status_prefix = f"⚠️ *Error ({code}):* `{cmd[:60]}`\n\n"
                                _update_ui(force=True)
                            elif event_name == "delta":
                                current_text += ev.get("delta", "")
                                _update_ui(force=False)
                            elif event_name == "done":
                                done_text = ev.get("text", "")
                                if done_text:
                                    current_text = done_text
                                _update_ui(force=True)
                            elif event_name == "error":
                                err_msg = ev.get("error", "Unknown error")
                                current_text = f"⚠️ Error: {err_msg}"
                                _update_ui(force=True)
                        except Exception as json_err:
                            print(f"[Telegram] Event error: {json_err}", file=sys.stderr)
                    else:
                        current_text += line + "\n"
                        _update_ui(force=False)

            client.close()
            _update_ui(force=True)

        except Exception as e:
            print(f"[Gateway] Socket communication warning: {e}", file=sys.stderr)
            if not current_text:
                fallback = f"👋 Hello {user_name}! Sayri received your message: '{prompt}'."
                if msg_id:
                    self.bot.edit_message_text(chat_id, msg_id, fallback)
                else:
                    self.bot.send_message(chat_id, fallback, reply_to_message_id=reply_to_msg_id)

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        user = message.get("from", {})
        user_id = str(user.get("id"))
        username = user.get("username")
        text = message.get("text", "").strip()

        if not text or not chat_id:
            return

        print(f"[Gateway] Message from @{username or user_id} (ID: {user_id}): {text}")

        # 1. Pairing Command (/pair ...)
        if text.startswith("/pair"):
            parts = text.split()
            if len(parts) > 1:
                pin_candidate = "".join(parts[1:]).replace(" ", "").replace("-", "").strip()
                ok, auth_reply = self.auth.verify_pairing_pin(pin_candidate, user_id, username)
                if ok:
                    self.bot.send_message(
                        chat_id,
                        f"🎉 {auth_reply}\nWelcome @{username or user_id}! You are now authorized to interact with Sayri.\n\nHow can I help you today?"
                    )
                    return
                else:
                    self.bot.send_message(
                        chat_id,
                        f"❌ {auth_reply}\nOpen Sayri on your Pulsar OS desktop, go to 'Gateways' -> 'Show Pairing PIN' and send /pair <PIN> here."
                    )
                    return
            else:
                self.bot.send_message(
                    chat_id,
                    "ℹ️ Usage: /pair <PIN> (e.g. /pair 123456)\nCheck the PIN in the Sayri desktop window."
                )
                return

        # 2. Check authorization for all other messages
        if not self.auth.is_authorized(user_id, username):
            welcome_locked = (
                "🔒 Sayri Desktop Assistant\n\n"
                "This assistant is private and locked to its desktop owner.\n\n"
                "👉 To authorize your Telegram account:\n"
                "1. Open Sayri on your Pulsar OS desktop.\n"
                "2. Go to 'Gateways' -> '🔑 Show Pairing PIN'.\n"
                "3. Copy the command and send it here: /pair <PIN>."
            )
            self.bot.send_message(chat_id, welcome_locked)
            return

        # 3. Dedicated Commands (/new, /resume, /start, /help)
        user_display = username or user.get("first_name", "User")
        now = time.time()

        if text.startswith(("/new", "/nuevo")):
            prev_id = self._sessions.get(chat_id, {}).get("session_id")
            new_id = f"tg-{INSTANCE_ID}-{chat_id}-{int(now)}"
            self._sessions[chat_id] = {
                "session_id": new_id,
                "last_activity": now,
                "prev_session_id": prev_id,
            }
            self.bot.send_message(chat_id, "✨ New conversation started. What would you like to talk about?")
            return

        if text.startswith(("/resume", "/continuar")):
            if not self.allow_resume_previous:
                self.bot.send_message(
                    chat_id,
                    "🔒 Resuming previous conversations is disabled in this Gateway's settings.\nYou can enable it from the Sayri desktop window."
                )
                return

            prev_id = self._sessions.get(chat_id, {}).get("prev_session_id")
            if prev_id:
                self._sessions[chat_id]["session_id"] = prev_id
                self._sessions[chat_id]["last_activity"] = now
                self.bot.send_message(
                    chat_id,
                    "🔄 Previous conversation resumed successfully. Continuing where we left off."
                )
            else:
                self.bot.send_message(
                    chat_id,
                    "ℹ️ No recent previous conversation found to resume in this session."
                )
            return

        if text in ("/start", "/help"):
            timeout_min = int(self.inactivity_timeout / 60)
            self.bot.send_message(
                chat_id,
                f"🤖 Sayri Copilot Active\n\n"
                f"Hello @{user_display}! Our conversation is continuous while we chat.\n\n"
                f"Useful commands:\n"
                f"• /new - Start a new conversation topic.\n"
                f"• /resume - Resume the previous conversation.\n\n"
                f"⏳ Conversations reset after {timeout_min} min of inactivity."
            )
            return

        # 4. Resolve active session and forward query with live streaming to Sayri Core
        session_id, was_new = self._get_or_create_session(chat_id, user_display)
        self.bot.send_chat_action(chat_id, "typing")
        self.query_sayri_core_stream(
            text,
            user_display,
            session_id=session_id,
            chat_id=chat_id,
            reply_to_msg_id=message.get("message_id"),
        )

    def run(self) -> None:
        print("[Sayri Telegram Gateway] Gateway started successfully.")
        print(f"[Sayri Telegram Gateway] Inactivity timeout: {self.inactivity_timeout}s | Allow Resume: {self.allow_resume_previous}")
        print("[Sayri Telegram Gateway] Listening for updates on api.telegram.org...")

        while self._running:
            try:
                updates = self.bot.get_updates(offset=self.last_update_id, timeout=20)
                for update in updates:
                    self.last_update_id = update["update_id"] + 1
                    if "message" in update:
                        self.handle_message(update["message"])
            except KeyboardInterrupt:
                print("\n[Gateway] Shutting down...")
                break
            except Exception as e:
                print(f"[Gateway] Polling loop exception: {e}", file=sys.stderr)
                time.sleep(3)


if __name__ == "__main__":
    gateway = SayriTelegramGateway()
    gateway.run()


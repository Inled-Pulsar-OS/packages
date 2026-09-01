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

    def send_message(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        payload = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        res = self._api_call("sendMessage", payload)
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
            return False, f"Demasiados intentos fallidos. Por favor, espera {cooldown_left} segundos antes de volver a intentar."

        clean_pin = pin.replace(" ", "").replace("-", "").strip()
        print(f"[Auth] 🔍 Verifying candidate PIN for user @{username} (ID: {user_id})...")
        if not clean_pin:
            return False, "Código PIN vacío."

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

                    return True, "¡Emparejamiento completado con éxito! Tu cuenta ha sido autorizada en Sayri."
                else:
                    self._failed_attempts[str(user_id)] = (fail_count + 1, now)
                    remaining = 5 - (fail_count + 1)
                    print(f"[Auth] ❌ PIN mismatch or expired for user {user_id}. Remaining attempts: {remaining}")
                    return False, f"PIN incorrecto o expirado. Intentos restantes: {max(0, remaining)}"
            except Exception as e:
                print(f"[Auth] Error checking shared pin file: {e}", file=sys.stderr)
        else:
            print(f"[Auth] ⚠️ PIN file not found at {SHARED_PIN_FILE}")

        self._failed_attempts[str(user_id)] = (fail_count + 1, now)
        return False, "No se encontró un PIN de emparejamiento activo en el escritorio."


class SayriTelegramGateway:
    """Main Gateway loop."""

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

    def query_sayri_core(self, prompt: str, user_name: str) -> str:
        """Sends query to Sayri IPC socket or evaluates locally."""
        sock_path = self.find_sayri_socket()
        if sock_path:
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(45.0)
                client.connect(str(sock_path))
                payload = {
                    "type": "INCOMING_MSG",
                    "author": user_name,
                    "text": prompt,
                    "target_agent": TARGET_AGENT,
                    "sandbox_level": SANDBOX_LEVEL,
                    "instance_id": INSTANCE_ID,
                }
                client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                
                chunks = []
                while True:
                    data = client.recv(4096)
                    if not data:
                        break
                    chunks.append(data.decode("utf-8", errors="replace"))
                client.close()
                response_data = "".join(chunks).strip()
                if response_data:
                    return response_data
            except Exception as e:
                print(f"[Gateway] Socket communication warning: {e}", file=sys.stderr)

        return f"👋 Hello {user_name}! Sayri received your message: '{prompt}'."

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
                        f"🎉 {auth_reply}\nBienvenido @{username or user_id}! Ya estás autorizado para interactuar con Sayri.\n\n¿En qué te puedo ayudar?"
                    )
                    return
                else:
                    self.bot.send_message(
                        chat_id,
                        f"❌ {auth_reply}\nAbre Sayri en tu escritorio de Pulsar OS, ve a 'Gateways' -> 'Show Pairing PIN' y envía /pair <PIN> aquí."
                    )
                    return
            else:
                self.bot.send_message(
                    chat_id,
                    "ℹ️ Uso: /pair <PIN> (ej. /pair 123456)\nConsulta el PIN en la ventana de Sayri en tu escritorio."
                )
                return

        # 2. Check authorization for all other messages (/start, /help, queries)
        if not self.auth.is_authorized(user_id, username):
            # NEVER reveal the PIN to the chat.
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

        # 3. Authorized user query
        if text in ("/start", "/help"):
            self.bot.send_message(
                chat_id,
                f"🤖 Sayri Copilot Active\nWelcome @{username or user_id}! How can I help you on your Pulsar OS system today?"
            )
            return

        # Forward query to Sayri Core
        self.bot.send_chat_action(chat_id, "typing")
        reply = self.query_sayri_core(text, username or user.get("first_name", "User"))
        self.bot.send_message(chat_id, reply, reply_to_message_id=message.get("message_id"))

    def run(self) -> None:
        print("[Sayri Telegram Gateway] Gateway started successfully.")
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

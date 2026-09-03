#!/usr/bin/env python3
"""Autonomous Sayri Gateway for Discord (Multi-Instance Channel Architecture).

Provides a rich, secure bridge between Discord server channels/DMs and Sayri AI Agents:
- Native Discord Slash Commands: `/sayri`, `/pair`, `/resume`, `/new`, `/guests`
- Continuous conversations with persistent context memory per channel/DM until `/new`
- Text mentions: `@Sayri <prompt>`, `!sayri <prompt>`, `!pair <pin>`, `!new`
- Channel guest access control with Desktop UI Kill Switch
- Strict DM isolation: Private DMs are exclusive to the paired desktop owner
- Channel history reading & summarization capabilities (`/resume`)
- Pure-python WebSocket / REST client with zero external dependencies
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import os
import random
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Multi-instance dynamic configuration
INSTANCE_ID = os.environ.get("SAYRI_GATEWAY_INSTANCE_ID", "sayri-gateway-discord")
TARGET_AGENT = os.environ.get("SAYRI_TARGET_AGENT", "default")
SANDBOX_LEVEL = os.environ.get("SAYRI_SANDBOX_LEVEL", "LEVEL_1_READONLY")

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "sayri"
PID_FILE = Path(os.environ.get("SAYRI_PID_FILE", str(DEFAULT_CONFIG_DIR / f"gateway_{INSTANCE_ID}.pid")))
AUTH_FILE = Path(os.environ.get("SAYRI_AUTH_FILE", str(DEFAULT_CONFIG_DIR / f"authorizations_{INSTANCE_ID}.json")))
SHARED_PIN_FILE = Path(os.environ.get("SAYRI_PIN_FILE", str(DEFAULT_CONFIG_DIR / f"pairing_pin_{INSTANCE_ID}.json")))

DISCORD_API_BASE = "https://discord.com/api/v10"
GATEWAY_HOST = "gateway.discord.gg"


class AuthorizationManager:
    """Enforces Desktop OTP Pairing, Channel Guest Access, and Kill Switch Controls."""

    def __init__(self):
        self.auth_file = AUTH_FILE
        self.whitelisted_users: Set[str] = set()
        self.allow_channel_guests: bool = False
        self.authorized_channels: Set[str] = set()
        self._failed_attempts: Dict[str, Tuple[int, float]] = {}
        self._load()

    def _load(self) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        if self.auth_file.is_file():
            try:
                data = json.loads(self.auth_file.read_text(encoding="utf-8"))
                self.whitelisted_users = set(str(u).lower() for u in data.get("allowed_discord_users", []))
                self.allow_channel_guests = bool(data.get("allow_channel_guests", False))
                self.authorized_channels = set(str(c) for c in data.get("authorized_channels", []))
            except Exception:
                pass

    def _save(self) -> None:
        try:
            self.auth_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": time.time(),
                "allowed_discord_users": list(self.whitelisted_users),
                "allow_channel_guests": self.allow_channel_guests,
                "authorized_channels": list(self.authorized_channels),
            }
            self.auth_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            try:
                os.chmod(self.auth_file, 0o600)
            except OSError:
                pass
        except Exception as e:
            print(f"[Auth] Save error: {e}", file=sys.stderr)

    def is_owner(self, user_id: str, username: Optional[str] = None) -> bool:
        self._load()
        u_id = str(user_id).lower()
        if u_id in self.whitelisted_users:
            return True
        if username:
            u_name = username.lower()
            if u_name in self.whitelisted_users or f"@{u_name}" in self.whitelisted_users:
                return True
        return False

    def can_interact(self, user_id: str, username: Optional[str], channel_id: str, is_dm: bool) -> Tuple[bool, str]:
        """Evaluates interaction permissions with strict DM isolation."""
        self._load()
        if self.is_owner(user_id, username):
            return True, "owner"

        if is_dm:
            return False, "🔒 **Acceso Denegado (DM)**\nSolo el propietario emparejado tiene permitido interactuar con Sayri por Mensaje Directo (DM).\nEn servidores, puedes hablarme en los canales autorizados si el propietario habilitó el acceso a invitados."

        if self.allow_channel_guests:
            return True, "guest"

        return False, "🔒 **Acceso de Invitados Desactivado**\nEl propietario de este asistente no ha habilitado el acceso a invitados en este canal. Solo el usuario emparejado puede interactuar con Sayri."

    def set_guests_allowed(self, enabled: bool) -> None:
        self._load()
        self.allow_channel_guests = enabled
        self._save()
        print(f"[Auth] 👥 Channel guests access set to: {enabled}")

    def verify_pairing_pin(self, pin: str, user_id: str, username: Optional[str] = None) -> Tuple[bool, str]:
        now = time.time()
        fail_count, last_fail = self._failed_attempts.get(str(user_id), (0, 0.0))
        if now - last_fail > 600:
            fail_count = 0
        if fail_count >= 5:
            cooldown_left = int(600 - (now - last_fail))
            print(f"[Auth Security] 🚨 Rate limit exceeded for user {user_id} ({cooldown_left}s remaining)")
            return False, f"Demasiados intentos fallidos. Espera {cooldown_left} segundos antes de volver a intentar."

        clean_pin = pin.replace(" ", "").replace("-", "").strip()
        print(f"[Auth] 🔍 Verifying candidate PIN for Discord user {username} (ID: {user_id})...")
        if not clean_pin:
            return False, "Código PIN vacío."

        if SHARED_PIN_FILE.is_file():
            try:
                data = json.loads(SHARED_PIN_FILE.read_text(encoding="utf-8"))
                file_pin = str(data.get("pin", "")).replace(" ", "").replace("-", "").strip()
                expires_at = data.get("expires_at", float("inf"))

                if hmac.compare_digest(clean_pin, file_pin) and time.time() <= expires_at:
                    self.whitelisted_users.add(str(user_id).lower())
                    if username:
                        self.whitelisted_users.add(username.lower())
                    self._save()
                    self._failed_attempts.pop(str(user_id), None)
                    print(f"✅ [Sayri Auth] Successfully paired Discord user @{username or user_id} ({user_id})!")

                    # Invalidate and rotate PIN after successful pairing
                    try:
                        new_pin = f"{random.randint(100000, 999999)}"
                        SHARED_PIN_FILE.write_text(json.dumps({
                            "pin": new_pin,
                            "created_at": time.time(),
                            "expires_at": time.time() + 86400,
                        }, indent=2), encoding="utf-8")
                        os.chmod(SHARED_PIN_FILE, 0o600)
                    except Exception:
                        pass

                    return True, "¡Emparejamiento completado con éxito! Tu cuenta de Discord ha sido autorizada como propietaria de Sayri."
                else:
                    self._failed_attempts[str(user_id)] = (fail_count + 1, now)
                    remaining = 5 - (fail_count + 1)
                    print(f"[Auth] ❌ PIN mismatch or expired for user {user_id}. Remaining attempts: {remaining}")
                    return False, f"PIN incorrecto o expirado. Intentos restantes: {max(0, remaining)}"
            except Exception as e:
                print(f"[Auth] Error checking pin file: {e}", file=sys.stderr)
        else:
            print(f"[Auth] ⚠️ PIN file not found at {SHARED_PIN_FILE}")

        self._failed_attempts[str(user_id)] = (fail_count + 1, now)
        return False, "No se encontró un PIN de emparejamiento activo en el escritorio."


class DiscordRestClient:
    """REST API wrapper for Discord API v10."""

    def __init__(self, token: str):
        self.token = token.strip()
        self.headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "SayriGateway (https://pulsar.inled.es, v1.0)",
            "Content-Type": "application/json",
        }

    def request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{DISCORD_API_BASE}{endpoint}"
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=payload, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 204:
                    return {}
                res_body = resp.read().decode("utf-8")
                return json.loads(res_body) if res_body else {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            print(f"[Discord REST] HTTP {exc.code} on {method} {endpoint}: {err_body}", file=sys.stderr)
            return None
        except Exception as exc:
            print(f"[Discord REST] Error on {method} {endpoint}: {exc}", file=sys.stderr)
            return None

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        return self.request("GET", "/users/@me")

    def register_slash_commands(self, application_id: str) -> None:
        """Registers global Discord Slash Commands (/sayri, /pair, /resume, /new, /guests)."""
        commands = [
            {
                "name": "sayri",
                "description": "Send a prompt or task to your Sayri AI Assistant",
                "options": [
                    {
                        "type": 3,  # STRING
                        "name": "message",
                        "description": "Message or query for Sayri",
                        "required": True,
                    }
                ],
            },
            {
                "name": "new",
                "description": "Start a new conversation and reset context memory for this channel",
            },
            {
                "name": "guests",
                "description": "Toggle or check channel guest access (Owner only)",
                "options": [
                    {
                        "type": 5,  # BOOLEAN
                        "name": "enabled",
                        "description": "Enable or disable guest access for server members",
                        "required": False,
                    }
                ],
            },
            {
                "name": "pair",
                "description": "Pair and authorize your Discord account with Sayri using desktop PIN",
                "options": [
                    {
                        "type": 3,  # STRING
                        "name": "pin",
                        "description": "6-digit OTP PIN shown on your Sayri desktop screen",
                        "required": True,
                    }
                ],
            },
            {
                "name": "resume",
                "description": "Summarize and analyze recent messages in this channel",
                "options": [
                    {
                        "type": 4,  # INTEGER
                        "name": "amount",
                        "description": "Number of recent messages to analyze (default: 20)",
                        "required": False,
                    }
                ],
            },
        ]
        res = self.request("PUT", f"/applications/{application_id}/commands", commands)
        if res is not None:
            print(f"✨ [Discord Gateway] Registered native Slash Commands (/sayri, /new, /guests, /pair, /resume) for app {application_id}")

    def respond_interaction_defer(self, interaction_id: str, interaction_token: str) -> None:
        """Acknowledges interaction with Type 5 (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE)."""
        self.request("POST", f"/interactions/{interaction_id}/{interaction_token}/callback", {"type": 5})

    def edit_interaction_response(self, application_id: str, interaction_token: str, content: str) -> None:
        """Edits the original deferred interaction response with the final answer."""
        max_len = 1900
        chunks = []
        text = content.strip()
        while len(text) > max_len:
            split_idx = text.rfind("\n", 0, max_len)
            if split_idx == -1 or split_idx < max_len // 2:
                split_idx = max_len
            chunks.append(text[:split_idx].strip())
            text = text[split_idx:].strip()
        if text:
            chunks.append(text)

        for i, chunk in enumerate(chunks):
            if i == 0:
                self.request("PATCH", f"/webhooks/{application_id}/{interaction_token}/messages/@original", {"content": chunk})
            else:
                self.request("POST", f"/webhooks/{application_id}/{interaction_token}", {"content": chunk})

    def send_message(self, channel_id: str, content: str, reply_to_message_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Sends a message to a Discord channel, splitting into chunks if necessary."""
        max_len = 1900
        chunks = []
        text = content.strip()
        while len(text) > max_len:
            split_idx = text.rfind("\n", 0, max_len)
            if split_idx == -1 or split_idx < max_len // 2:
                split_idx = max_len
            chunks.append(text[:split_idx].strip())
            text = text[split_idx:].strip()
        if text:
            chunks.append(text)

        last_resp = None
        for i, chunk in enumerate(chunks):
            data: Dict[str, Any] = {"content": chunk}
            if i == 0 and reply_to_message_id:
                data["message_reference"] = {
                    "message_id": reply_to_message_id,
                    "fail_if_not_exists": False,
                }
            last_resp = self.request("POST", f"/channels/{channel_id}/messages", data)
        return last_resp

    def get_channel_messages(self, channel_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetches recent messages from a channel for summarization."""
        limit = max(1, min(limit, 100))
        res = self.request("GET", f"/channels/{channel_id}/messages?limit={limit}")
        return res if isinstance(res, list) else []

    def trigger_typing(self, channel_id: str) -> None:
        """Sends typing indicator to channel."""
        try:
            self.request("POST", f"/channels/{channel_id}/typing")
        except Exception:
            pass


class RawWebSocketClient:
    """Pure-Python compliant WebSocket client for Discord Gateway v10."""

    def __init__(self, host: str, path: str = "/?v=10&encoding=json"):
        self.host = host
        self.path = path
        self.sock: Optional[ssl.SSLSocket] = None
        self.connected = False

    def connect(self) -> None:
        raw_sock = socket.create_connection((self.host, 443), timeout=20)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw_sock, server_hostname=self.host)

        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: SayriGateway/1.0\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))

        header_data = b""
        while b"\r\n\r\n" not in header_data:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed (closed during headers)")
            header_data += chunk

        lines = header_data.split(b"\r\n")
        status_line = lines[0].decode("ascii", errors="replace")
        if " 101 " not in status_line:
            raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

        self.connected = True
        self.sock.settimeout(60.0)

    def send_text(self, text: str) -> None:
        if not self.sock or not self.connected:
            raise ConnectionError("Socket not connected")
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        frame = bytearray([0x81])
        length = len(payload)
        if length <= 125:
            frame.append(0x80 | length)
        elif length <= 65535:
            frame.append(0x80 | 126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack("!Q", length))

        frame.extend(mask)
        frame.extend(masked)
        self.sock.sendall(frame)

    def recv_frame(self) -> Tuple[int, bytes]:
        if not self.sock:
            raise ConnectionError("Socket closed")
        header = self._recv_exact(2)
        b1, b2 = header[0], header[1]
        opcode = b1 & 0x0F
        is_masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F

        if payload_len == 126:
            payload_len = struct.unpack("!H", self._recv_exact(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack("!Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if is_masked else None
        data = self._recv_exact(payload_len)
        if is_masked and mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Remote peer closed connection")
            buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


class DiscordGatewayDaemon:
    """Main daemon bridging Discord Gateway WebSocket and Sayri core socket."""

    def __init__(self, token: str):
        self.token = token.strip()
        self.rest = DiscordRestClient(self.token)
        self.auth = AuthorizationManager()
        self.ws: Optional[RawWebSocketClient] = None
        self.heartbeat_interval = 41.25
        self.last_sequence: Optional[int] = None
        self.session_id: Optional[str] = None
        self.running = False
        # Configuration from Gateway Supervisor
        self.allow_resume_previous = os.environ.get("SAYRI_ALLOW_RESUME_PREVIOUS", "1") == "1"
        try:
            self.inactivity_timeout = float(os.environ.get("SAYRI_INACTIVITY_TIMEOUT", "1800"))
        except ValueError:
            self.inactivity_timeout = 1800.0  # Default 30 minutes

        # Session versioning and activity tracking for continuous memory and /new
        self.channel_sessions: Dict[str, int] = {}
        self.channel_last_activity: Dict[str, float] = {}

    def get_channel_session_id(self, channel_id: str, author_id: str, is_dm: bool) -> str:
        """Builds a deterministic continuous session ID per channel or DM, advancing on standby timeout."""
        key = f"dm:{author_id}" if is_dm else f"chan:{channel_id}"
        now = time.time()
        last_act = self.channel_last_activity.get(key, now)

        if key in self.channel_last_activity and (now - last_act > self.inactivity_timeout):
            # Inactivity timeout reached: automatically increment epoch to start clean new conversation
            self.channel_sessions[key] = self.channel_sessions.get(key, 0) + 1
            print(f"[Discord Gateway] ⏱️ Inactivity timeout reached for {key}. Advanced to session epoch {self.channel_sessions[key]}")

        self.channel_last_activity[key] = now
        epoch = self.channel_sessions.get(key, 0)
        return f"remote-discord-{key}-{epoch}"

    def reset_channel_session(self, channel_id: str, author_id: str, is_dm: bool) -> None:
        """Increments session epoch to reset context memory."""
        key = f"dm:{author_id}" if is_dm else f"chan:{channel_id}"
        self.channel_sessions[key] = self.channel_sessions.get(key, 0) + 1
        self.channel_last_activity[key] = time.time()
        print(f"[Discord Gateway] 🔄 Reset context memory for {key} (New epoch: {self.channel_sessions[key]})")

    def query_sayri_core(self, prompt: str, user_name: str, session_id: Optional[str] = None) -> str:
        """Dispatches message to Sayri Core over local UNIX domain socket."""
        candidate_sockets = [
            Path.home() / ".local" / "share" / "sayri" / "sayri.sock",
            Path(f"/run/user/{os.getuid()}/sayri.sock"),
            Path.home() / ".config" / "sayri" / "sayri.sock",
            Path("/tmp/sayri.sock"),
        ]

        sock_path = None
        for cand in candidate_sockets:
            if cand.is_socket():
                sock_path = cand
                break

        if sock_path:
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(35.0)
                client.connect(str(sock_path))

                payload = {
                    "type": "INCOMING_MSG",
                    "text": prompt,
                    "author": f"@{user_name}",
                    "channel": "discord",
                    "target_agent": TARGET_AGENT,
                    "sandbox_level": SANDBOX_LEVEL,
                    "instance_id": INSTANCE_ID,
                    "session_id": session_id,
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
                print(f"[Discord Gateway] Socket communication error on {sock_path}: {e}", file=sys.stderr)

        return f"👋 Hola {user_name}! Sayri ha recibido tu mensaje: '{prompt}'."

    def handle_discord_interaction(self, interaction: Dict[str, Any], bot_user: Dict[str, Any]) -> None:
        """Handles native Discord Slash Commands (/sayri, /new, /guests, /pair, /resume)."""
        inter_id = interaction.get("id")
        inter_token = interaction.get("token")
        app_id = interaction.get("application_id", str(bot_user.get("id", "")))
        channel_id = interaction.get("channel_id", "")
        guild_id = interaction.get("guild_id")
        is_dm = guild_id is None

        user_info = interaction.get("member", {}).get("user") or interaction.get("user", {})
        user_id = str(user_info.get("id"))
        username = user_info.get("username", "User")

        data = interaction.get("data", {})
        cmd_name = data.get("name")
        options = {opt["name"]: opt.get("value") for opt in data.get("options", [])}

        print(f"[Discord Gateway] Slash Command /{cmd_name} from @{username} (ID: {user_id}) in #{channel_id}")

        # 1. Acknowledge with Deferred response ("Sayri está pensando...")
        self.rest.respond_interaction_defer(inter_id, inter_token)

        # 2. Command: /pair <pin>
        if cmd_name == "pair":
            pin_val = str(options.get("pin", "")).strip()
            ok, auth_reply = self.auth.verify_pairing_pin(pin_val, user_id, username)
            if ok:
                msg = f"🎉 **{auth_reply}**\n¡Bienvenido <@{user_id}>! Ya estás autorizado como propietario de Sayri.\n\n¿En qué te puedo ayudar hoy?"
            else:
                msg = f"❌ **{auth_reply}**\nAbre Sayri en tu escritorio de Pulsar OS, ve a 'Gateways' -> 'Show Pairing PIN' y usa `/pair <PIN>`."
            self.rest.edit_interaction_response(app_id, inter_token, msg)
            return

        # 3. Command: /guests [enabled] (Owner only)
        if cmd_name == "guests":
            if not self.auth.is_owner(user_id, username):
                self.rest.edit_interaction_response(
                    app_id, inter_token, "🔒 **Solo el propietario emparejado** puede configurar el acceso a invitados."
                )
                return
            if "enabled" in options:
                val = bool(options["enabled"])
                self.auth.set_guests_allowed(val)
                state = "🟢 **Activado**" if val else "🔴 **Desactivado (Kill Switch Activo)**"
                self.rest.edit_interaction_response(
                    app_id, inter_token, f"👥 **Acceso de Invitados**: {state}\nLos miembros del servidor {'ahora pueden' if val else 'ya no pueden'} interactuar con Sayri en canales públicos."
                )
            else:
                current = "🟢 **Activado**" if self.auth.allow_channel_guests else "🔴 **Desactivado (Solo Propietario)**"
                self.rest.edit_interaction_response(
                    app_id, inter_token, f"ℹ️ **Estado de Invitados**: {current}\nUsa `/guests enabled:True` o `/guests enabled:False` para cambiarlo."
                )
            return

        # 4. Command: /new (Reset conversation context)
        if cmd_name == "new":
            allowed, reason = self.auth.can_interact(user_id, username, channel_id, is_dm)
            if not allowed:
                self.rest.edit_interaction_response(app_id, inter_token, reason)
                return

            self.reset_channel_session(channel_id, user_id, is_dm)
            self.rest.edit_interaction_response(
                app_id, inter_token, "🔄 **Nueva conversación iniciada.**\nHe reseteado la memoria de este chat. ¿En qué puedo ayudarte ahora?"
            )
            return

        # 5. Check Authorization for /sayri and /resume
        allowed, reason = self.auth.can_interact(user_id, username, channel_id, is_dm)
        if not allowed:
            self.rest.edit_interaction_response(app_id, inter_token, reason)
            return

        # 6. Command: /resume [amount]
        if cmd_name in ("resume", "resumen"):
            limit = int(options.get("amount") or options.get("cantidad") or 20)
            recent_msgs = self.rest.get_channel_messages(channel_id, limit=limit)
            if recent_msgs:
                history_lines = []
                for m in reversed(recent_msgs):
                    m_author = m.get("author", {}).get("username", "Usuario")
                    m_text = m.get("content", "").strip()
                    if m_text and not m.get("author", {}).get("bot"):
                        history_lines.append(f"- {m_author}: {m_text}")
                history_context = "\n".join(history_lines)
                prompt = (
                    f"[Historial reciente del canal de Discord #{channel_id} (últimos {len(history_lines)} mensajes)]:\n"
                    f"{history_context}\n\n"
                    f"[Instrucción]: Genera un resumen claro, estructurado y conciso de lo conversado en el canal."
                )
            else:
                prompt = "Genera un breve resumen de bienvenida al canal."

            session_id = self.get_channel_session_id(channel_id, user_id, is_dm)
            reply = self.query_sayri_core(prompt, username, session_id=session_id)
            self.rest.edit_interaction_response(app_id, inter_token, reply)
            return

        # 7. Command: /sayri <message>
        if cmd_name == "sayri":
            prompt = str(options.get("message") or options.get("mensaje") or "").strip()
            summary_triggers = ["resume", "resumen", "qué han dicho", "que han dicho", "lee los mensajes", "lee el canal", "últimos mensajes"]
            if any(trig in prompt.lower() for trig in summary_triggers):
                recent_msgs = self.rest.get_channel_messages(channel_id, limit=20)
                if recent_msgs:
                    history_lines = []
                    for m in reversed(recent_msgs):
                        m_author = m.get("author", {}).get("username", "Usuario")
                        m_text = m.get("content", "").strip()
                        if m_text and not m.get("author", {}).get("bot"):
                            history_lines.append(f"- {m_author}: {m_text}")
                    if history_lines:
                        prompt = (
                            f"[Historial reciente del canal de Discord #{channel_id}]:\n"
                            f"{chr(10).join(history_lines)}\n\n"
                            f"[Instrucción del usuario @{username}]:\n{prompt}"
                        )

            session_id = self.get_channel_session_id(channel_id, user_id, is_dm)
            reply = self.query_sayri_core(prompt, username, session_id=session_id)
            
            # Format public response showing author question and Sayri reply
            final_formatted = f"> **@{username}**: {prompt}\n\n{reply}" if not is_dm else reply
            self.rest.edit_interaction_response(app_id, inter_token, final_formatted)
            return

    def handle_discord_message(self, message: Dict[str, Any], bot_user: Dict[str, Any]) -> None:
        """Text, mentions (@Sayri), and commands (!new, !pair) fallback handler."""
        author = message.get("author", {})
        if author.get("bot"):
            return

        channel_id = message.get("channel_id")
        msg_id = message.get("id")
        user_id = str(author.get("id"))
        username = author.get("username", "User")
        guild_id = message.get("guild_id")
        content = message.get("content", "").strip()

        if not content or not channel_id:
            return

        bot_id = str(bot_user.get("id") or self.bot_user.get("id") or "")
        bot_name = str(bot_user.get("username") or self.bot_user.get("username") or "sayri").lower()
        is_dm = guild_id is None

        # 1. Comprehensive mention, role mention, reply, and trigger detection
        mentions_list = message.get("mentions", [])
        mention_roles = message.get("mention_roles", [])
        referenced_msg = message.get("referenced_message") or {}
        is_reply_to_bot = bool(bot_id and str(referenced_msg.get("author", {}).get("id")) == bot_id)

        is_bot_mentioned = any(str(m.get("id")) == bot_id for m in mentions_list)
        has_mention_tag = (f"<@{bot_id}>" in content) or (f"<@!{bot_id}>" in content)
        has_role_mention = bool(mention_roles) or bool(re.search(r"<@&\d+>", content))
        is_sayri_cmd = content.startswith(("/sayri", "!sayri", "/pair", "!pair", "/new", "!new", "/guests", "!guests"))
        names_sayri = bool(re.search(rf"\b@?{re.escape(bot_name)}\b", content, re.IGNORECASE)) or bool(re.search(r"\b@?sayri\b", content, re.IGNORECASE))

        is_addressed = is_dm or is_bot_mentioned or has_mention_tag or has_role_mention or is_reply_to_bot or is_sayri_cmd or names_sayri
        if not is_addressed:
            return

        # 2. Clean prompt text
        clean_text = content
        clean_text = re.sub(r"<@&\d+>", "", clean_text)
        if bot_id:
            clean_text = re.sub(rf"<@!?{bot_id}>", "", clean_text)
        clean_text = re.sub(r"<@!?\d+>", "", clean_text)
        clean_text = re.sub(r"^/sayri\s*", "", clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r"^!sayri\s*", "", clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(rf"^@?{re.escape(bot_name)}[:,]?\s*", "", clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r"^@?sayri[:,]?\s*", "", clean_text, flags=re.IGNORECASE).strip()

        if not clean_text:
            clean_text = "Hola Sayri, ¿en qué puedes ayudarme?"

        print(f"[Discord Gateway] 📩 Query from @{username} (ID: {user_id}) in #{channel_id}: '{clean_text}'")

        # 3. Pairing Command (/pair <PIN> o !pair <PIN>)
        if clean_text.startswith(("/pair", "!pair", "pair")):
            parts = clean_text.split()
            if len(parts) > 1:
                pin_candidate = "".join(parts[1:]).replace(" ", "").replace("-", "").strip()
                ok, auth_reply = self.auth.verify_pairing_pin(pin_candidate, user_id, username)
                if ok:
                    self.rest.send_message(
                        channel_id,
                        f"🎉 **{auth_reply}**\n¡Bienvenido <@{user_id}>! Ya estás autorizado como propietario de Sayri.\n\n¿En qué te puedo ayudar hoy?",
                        reply_to_message_id=msg_id,
                    )
                else:
                    self.rest.send_message(
                        channel_id,
                        f"❌ **{auth_reply}**\nAbre Sayri en tu escritorio de Pulsar OS, ve a 'Gateways' -> 'Show Pairing PIN' y escribe `/pair <PIN>`.",
                        reply_to_message_id=msg_id,
                    )
            else:
                self.rest.send_message(
                    channel_id,
                    "ℹ️ **Uso**: `/pair <PIN>` (ej. `/pair 123456`)\nConsulta el PIN en la ventana de Sayri en tu escritorio.",
                    reply_to_message_id=msg_id,
                )
            return

        # 4. Reset Command (/new o !new)
        if clean_text.startswith(("/new", "!new", "new")):
            allowed, reason = self.auth.can_interact(user_id, username, channel_id, is_dm)
            if not allowed:
                self.rest.send_message(channel_id, reason, reply_to_message_id=msg_id)
                return
            self.reset_channel_session(channel_id, user_id, is_dm)
            self.rest.send_message(
                channel_id,
                "🔄 **Nueva conversación iniciada.**\nHe reseteado la memoria de este chat. ¿En qué puedo ayudarte ahora?",
                reply_to_message_id=msg_id,
            )
            return

        # 5. Guests Command (/guests o !guests)
        if clean_text.startswith(("/guests", "!guests")):
            if not self.auth.is_owner(user_id, username):
                self.rest.send_message(channel_id, "🔒 **Solo el propietario emparejado** puede configurar el acceso a invitados.", reply_to_message_id=msg_id)
                return
            parts = clean_text.split()
            if len(parts) > 1:
                val = parts[1].lower() in ("on", "true", "1", "activar", "si", "sí")
                self.auth.set_guests_allowed(val)
                state = "🟢 **Activado**" if val else "🔴 **Desactivado (Kill Switch Activo)**"
                self.rest.send_message(channel_id, f"👥 **Acceso de Invitados**: {state}\nLos miembros del servidor {'ahora pueden' if val else 'ya no pueden'} interactuar con Sayri en canales públicos.", reply_to_message_id=msg_id)
            else:
                current = "🟢 **Activado**" if self.auth.allow_channel_guests else "🔴 **Desactivado (Solo Propietario)**"
                self.rest.send_message(channel_id, f"ℹ️ **Estado de Invitados**: {current}\nUsa `!guests on` o `!guests off` para cambiarlo.", reply_to_message_id=msg_id)
            return

        # 6. Check interaction authorization with strict DM protection
        allowed, reason = self.auth.can_interact(user_id, username, channel_id, is_dm)
        if not allowed:
            self.rest.send_message(channel_id, reason, reply_to_message_id=msg_id)
            return

        # Trigger visual typing indicator in Discord
        self.rest.trigger_typing(channel_id)

        # 7. Summarization auto-trigger in text
        prompt = clean_text
        summary_triggers = ["resume", "resumen", "qué han dicho", "que han dicho", "lee los mensajes", "lee el canal", "últimos mensajes"]
        if any(trig in prompt.lower() for trig in summary_triggers) and not is_dm:
            recent_msgs = self.rest.get_channel_messages(channel_id, limit=20)
            if recent_msgs:
                history_lines = []
                for m in reversed(recent_msgs):
                    m_author = m.get("author", {}).get("username", "Usuario")
                    m_text = m.get("content", "").strip()
                    if m_text and not m.get("author", {}).get("bot") and m.get("id") != msg_id:
                        history_lines.append(f"- {m_author}: {m_text}")
                if history_lines:
                    prompt = (
                        f"[Historial reciente del canal de Discord #{channel_id}]:\n"
                        f"{chr(10).join(history_lines)}\n\n"
                        f"[Instrucción del usuario @{username}]:\n{clean_text}"
                    )

        # 8. Continuous conversation with Sayri Core
        session_id = self.get_channel_session_id(channel_id, user_id, is_dm)
        reply = self.query_sayri_core(prompt, username, session_id=session_id)
        self.rest.send_message(channel_id, reply, reply_to_message_id=msg_id)

    def run(self) -> None:
        """Main connection and dispatch loop."""
        self.bot_user = self.rest.get_current_user() or {}
        if not self.bot_user or not self.bot_user.get("id"):
            print("[Discord Gateway] ❌ Failed to authenticate with Discord API. Verify bot token.", file=sys.stderr)
            sys.exit(1)

        bot_id = self.bot_user["id"]
        bot_tag = f"{self.bot_user.get('username')}#{self.bot_user.get('discriminator', '0')}"
        print(f"🤖 [Discord Gateway] Authenticated as @{bot_tag} (ID: {bot_id})")

        try:
            self.rest.register_slash_commands(str(bot_id))
        except Exception as e:
            print(f"[Discord Gateway] Slash command registration notice: {e}")

        # Write PID file
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

        self.running = True
        backoff = 2

        while self.running:
            try:
                print(f"[Discord Gateway] 🌐 Connecting to Discord Gateway WebSocket ({GATEWAY_HOST})...")
                self.ws = RawWebSocketClient(GATEWAY_HOST, "/?v=10&encoding=json")
                self.ws.connect()
                backoff = 2

                while self.running and self.ws.connected:
                    try:
                        opcode, data = self.ws.recv_frame()
                    except (socket.timeout, TimeoutError):
                        continue

                    if opcode == 0x8:  # CLOSE
                        print("[Discord Gateway] Gateway received CLOSE frame from Discord.")
                        break
                    elif opcode == 0x9:  # PING
                        if self.ws:
                            self.ws.sock.sendall(bytearray([0x8A, 0x80]) + os.urandom(4))
                        continue

                    if opcode == 0x1:  # TEXT
                        msg = json.loads(data.decode("utf-8"))
                        op = msg.get("op")
                        seq = msg.get("s")
                        event_type = msg.get("t")
                        event_data = msg.get("d")

                        if seq is not None:
                            self.last_sequence = seq

                        if op == 10:  # HELLO
                            self.heartbeat_interval = event_data.get("heartbeat_interval", 41250) / 1000.0
                            print(f"[Discord Gateway] HELLO received. Heartbeat interval: {self.heartbeat_interval}s")
                            threading.Thread(target=self._heartbeat_loop, daemon=True).start()

                            # Send IDENTIFY payload
                            # Intents: GUILDS (1) + GUILD_MESSAGES (512) + DIRECT_MESSAGES (4096) + MESSAGE_CONTENT (32768)
                            identify_payload = {
                                "op": 2,
                                "d": {
                                    "token": self.token,
                                    "intents": 1 | 512 | 4096 | 32768,
                                    "properties": {
                                        "os": "linux",
                                        "browser": "sayri-gateway",
                                        "device": "pulsar-os",
                                    },
                                },
                            }
                            self.ws.send_text(json.dumps(identify_payload))
                            print("✨ [Discord Gateway] Sent IDENTIFY payload with Intents (Guilds, DMs, Message Content).")

                        elif op == 0:  # DISPATCH
                            if event_type == "READY":
                                self.session_id = event_data.get("session_id")
                                print(f"🚀 [Discord Gateway] READY! Connected to Discord Gateway as @{bot_tag}")

                            elif event_type == "INTERACTION_CREATE":
                                threading.Thread(
                                    target=self.handle_discord_interaction,
                                    args=(event_data, self.bot_user),
                                    daemon=True,
                                ).start()

                            elif event_type == "MESSAGE_CREATE":
                                threading.Thread(
                                    target=self.handle_discord_message,
                                    args=(event_data, self.bot_user),
                                    daemon=True,
                                ).start()

                        elif op == 7:  # RECONNECT
                            print("[Discord Gateway] Reconnect requested by Discord.")
                            break
                        elif op == 9:  # INVALID_SESSION
                            print("[Discord Gateway] Invalid session. Reconnecting...")
                            time.sleep(2)
                            break

            except Exception as exc:
                print(f"[Discord Gateway] Connection notice: {exc}", file=sys.stderr)
            finally:
                if self.ws:
                    self.ws.close()

            if self.running:
                print(f"[Discord Gateway] Reconnecting in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _heartbeat_loop(self) -> None:
        while self.running and self.ws and self.ws.connected:
            try:
                hb = {"op": 1, "d": self.last_sequence}
                self.ws.send_text(json.dumps(hb))
            except Exception:
                break
            time.sleep(self.heartbeat_interval)


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("TOKEN_SAYRI_GATEWA_DISCORD_BOT_GATE_1364")
    if not token:
        # Check ~/.config/sayri/secrets.json
        secrets_file = DEFAULT_CONFIG_DIR / "secrets.json"
        if secrets_file.is_file():
            try:
                data = json.loads(secrets_file.read_text(encoding="utf-8"))
                for k, v in data.get("secrets", {}).items():
                    if "DISCORD" in k.upper() and v.get("value"):
                        token = v.get("value")
                        break
            except Exception:
                pass

    if not token:
        print("[Discord Gateway] ❌ Error: No Discord bot token found in environment or secrets vault.", file=sys.stderr)
        sys.exit(1)

    daemon = DiscordGatewayDaemon(token)
    try:
        daemon.run()
    except KeyboardInterrupt:
        print("\n[Discord Gateway] Shutting down cleanly.")
        daemon.running = False
        if PID_FILE.is_file():
            try:
                PID_FILE.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()

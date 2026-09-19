# Sayri Telegram Gateway Plugin

Autonomous background daemon bridging Sayri AI Copilot to Telegram channels with **Desktop-Only OTP Pairing** and **Zero-Plaintext Vault Security**.

New in v1.2.0: **Sayri xui UI** — terminal wizard (`panel.py wizard`) and local browser panel (`panel.py serve`) to configure the bot token (falls back to `~/.config/sayri/secrets.json` when `TELEGRAM_BOT_TOKEN` is not set), and to view/rotate the pairing PIN. UI is decoupled from the daemon.

## Architecture & Security Model

```
┌──────────────────────────────────────────────────────────┐
│ Telegram Cloud (api.telegram.org)                        │
└───────────────▲──────────────────────────┬───────────────┘
                │ HTTPS Long-Polling       │ Incoming Update
                │ (Token in Memory)        ▼
┌───────────────┴──────────────────────────────────────────┐
│ sayri-gateway-telegram (gateway.py Daemon)               │
│ - Zero-Leakage: Never reveals pairing PIN to chat       │
│ - Verifies PIN against ~/.config/sayri/pairing_pin.json  │
│ - Saves authorized user IDs to authorizations.json       │
└──────────────────────────┬───────────────────────────────┘
                           │ UNIX Domain Socket (/run/user/...)
                           │ JSON IPC: {"type": "INCOMING_MSG"}
                           ▼
┌──────────────────────────────────────────────────────────┐
│ Sayri Core (app.py & AgentEngine)                        │
│ - Processes query with Active Agent Profile & Tools      │
│ - Evaluates with LLM Provider and returns response       │
└──────────────────────────────────────────────────────────┘
```

## Security Guarantees
1. **Zero-Plaintext Token Shield**: `TELEGRAM_BOT_TOKEN` is never stored in chat logs or prompt contexts; it is loaded directly from the Zero-Plaintext Vault.
2. **Desktop-Only OTP Pairing**: The 6-digit PIN is **never** sent or echoed to the Telegram chat. It is generated and displayed exclusively on the local Sayri desktop screen (`Gateways -> Show Pairing PIN`).
3. **Flexible Command Parsing**: Accepts `/pair 123456`, `/pair 123 456`, or `/pair 123-456`.
4. **Instant Whitelisting**: On verification, user ID and username are stored in `~/.config/sayri/authorizations.json`.

## Quick Setup
1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and obtain your Bot API Token.
2. In Sayri Cajita, open **Gateways -> Set TELEGRAM_BOT_TOKEN** and save your token. Sayri automatically launches the supervisor daemon.
3. Click **Show Pairing PIN** to view and 1-click copy your pairing command.
4. Send `/pair <PIN>` to your bot in Telegram.
5. Your Telegram account is paired and you can query Sayri from anywhere.

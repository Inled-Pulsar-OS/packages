# Sayri Gateway Plugin: Discord Bot

This plugin connects any **Discord** server or direct message (DM) channel to your AI agents in **Sayri**, the personal AI assistant.

---

## Key Features

- **Sayri xui UI** (new in v1.2.0): terminal wizard (`sayri-discord wizard`) and local browser panel (`sayri-discord serve`) to configure the token, toggle guests, view and rotate pairing PIN — all decoupled from the daemon.
- **Flexible Invocation**:
  - In server channels: `/sayri <message>`, `!sayri <message>`, or mention the bot `@SayriBot <message>`.
  - In DMs: Just type your question and the bot will respond.
- **Smart Channel Reading & Summarization**:
  - Ask Sayri to *"resume the last messages"*, *"what was said above"*, or *"summarize this channel"* -- the gateway fetches recent messages via Discord's REST API and provides full context to the agent.
- **Secure Desktop Pairing (OTP)**:
  - Protection against unauthorized access with a 6-digit PIN generated on your desktop.
  - Brute-force protection with attempt limits and automatic PIN rotation.
- **Multi-Instance & Sandboxing**:
  - Create multiple bot instances connected to different agents (e.g. *Main Sayri*, *Developer Assistant*) with different isolation levels (`LEVEL_0_NO_EXEC` to `LEVEL_3_HOST_USER`).
- **Zero External Dependencies**:
  - Pure Python implementation with RFC 6455 WebSockets over native TLS and REST API v10.

---

## Step-by-Step Setup Guide

### Step 1: Create the Discord Application & Bot
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **`New Application`** in the top right and give it a name (e.g. `Sayri Assistant`).
3. In the left sidebar, go to the **`Bot`** tab.
4. Click **`Reset Token`** (or *Copy*), copy the **Bot Token** and save it (you'll need it in Sayri).

### Step 2: Enable Privileged Gateway Intents (Required!)
1. In the same **`Bot`** tab, scroll down to **`Privileged Gateway Intents`**.
2. **Enable the following**:
   - **`MESSAGE CONTENT INTENT`** *(Required for the bot to read `/sayri <message>` text in channels)*.
   - **`SERVER MEMBERS INTENT`** *(Recommended for identifying server users)*.
3. Click **`Save Changes`** at the bottom.

### Step 3: Generate the Bot Invitation URL
1. In the left sidebar, go to **`OAuth2`** -> **`URL Generator`**.
2. Under **`SCOPES`**, check only:
   - **`bot`**
3. Under **`BOT PERMISSIONS`**, check:
   - **`Send Messages`**
   - **`Send Messages in Threads`**
   - **`Read Message History`** *(Required for channel summarization)*
   - **`View Channels`**
   - **`Use External Emojis`** *(Optional)*
4. Copy the **`GENERATED URL`** at the bottom.
5. Paste it in your browser and select the Discord server to invite the bot.

---

## Configuration in **Sayri**, the personal AI assistant

1. Open Sayri and click the **Settings** button.
2. Go to the **Gateways** tab and click **`+ Add Gateway`**.
3. Fill in the form:
   - **Platform**: Select `Discord Bot Gateway (sayri-gateway-discord)`.
   - **Instance Name**: e.g. `Discord - Main Server`.
   - **Linked Agent**: Select the agent (e.g. `Sayri Main`).
   - **Sandbox Level**: Select the security level (e.g. `LEVEL_1_READONLY`).
   - **Bot Token**: Paste the token you copied in Step 1.
4. Click **`Create Gateway Instance`**. The bot will connect immediately.

---

## Pair Your Discord Account

For security, Sayri rejects messages from unknown users until they pair with the desktop:

1. In Sayri, on your Discord Gateway card, click **`Show Pairing PIN`** (shows a 6-digit code).
2. In your Discord server or DM with the bot, type:
   ```text
   /sayri /pair 123456
   ```
   *(Replace `123456` with the PIN shown on your screen.)*
3. The bot will confirm your account is authorized. You can now chat with Sayri freely!

---

## Available Discord Commands

| Command | Description |
| :--- | :--- |
| `/sayri <question>` | Send a query or request to Sayri. |
| `!sayri <question>` | Alternative prefix to query Sayri. |
| `@SayriBot <question>` | Direct mention to the bot in any channel. |
| `/sayri resume the last messages` | Read recent channel messages and generate a structured summary. |
| `/sayri /pair <PIN>` | Pair and authorize your Discord account with Sayri. |

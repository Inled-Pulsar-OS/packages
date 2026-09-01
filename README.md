# Pulsar OS Packages

Declarative packages, gateways, and plugins for **Pulsar OS**.

This repository contains the source code for Sayri AI gateways, skills, plugins, and other system packages that are published to the [Pulsar Store](https://store-os.inled.es).

---

## Repository Structure

```
packages/
├── plugins/
│   ├── sayri-gateway-telegram/    # Telegram Bot Gateway for Sayri
│   └── sayri-gateway-discord/     # Discord Bot Gateway for Sayri
├── skills/                        # Sayri AI skills (coming soon)
└── README.md
```

---

## Available Gateways

| Package | Description | Version |
|:---|:---|:---:|
| **Telegram Bot Gateway** | Bridges Sayri with Telegram chats via OTP pairing | `v1.1.0` |
| **Discord Bot Gateway** | Connects Discord servers/DMs to Sayri with conversation memory and guest controls | `v1.1.0` |

---

## Installation

Install packages from the Pulsar Store CLI:

```bash
pulsar-store install sayri-gateway-telegram
pulsar-store install sayri-gateway-discord
```

Or via the [Pulsar Store web UI](https://store-os.inled.es).

---

## Development

### Gateway Structure

Each gateway plugin follows this layout:

```
sayri-gateway-<platform>/
├── manifest.json       # Plugin manifest (id, auth, capabilities)
├── gateway.py          # Main entrypoint daemon
├── requirements.txt    # Python dependencies
└── README.md           # Documentation
```

### Requirements

- Python 3.10+
- Pulsar OS with Sayri installed

### Running locally

```bash
cd plugins/sayri-gateway-telegram
pip install -r requirements.txt
python gateway.py
```

---

## Submitting to Pulsar Store

1. Package your gateway as a `.zip` file
2. Upload to a public URL (GitHub Releases recommended)
3. Open an issue on the [Pulsar Store repo](https://github.com/Inled-Pulsar-OS/store/issues/new?template=submit-plugin.yml)
4. The automated pipeline will audit and publish your package

---

## Security

All gateways undergo **double-layer security auditing**:

- **VirusTotal** malware scan (zero-tolerance)
- **OpenCode AI** semantic code audit

Gateways run in sandboxed environments with strict authorization (OTP pairing, rate limiting, domain whitelists).

---

## License

Part of the [Pulsar OS](https://os.inled.es) project by [Inled](https://inled.es).

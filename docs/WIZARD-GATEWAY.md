# OpenClaw Wizard Gateway — Installation & Architecture

## What It Is

The SaaSClaw wizard uses **[OpenClaw](https://github.com/openclaw/openclaw)** as its LLM gateway. When a user chats with the wizard in the browser, messages flow through the Django app → OpenClaw wizard gateway → LLM provider (Z.AI, OpenAI, etc.). The wizard gateway handles model routing, session management, and provides a standardized chat completions API that the Django engine consumes.

This is **separate** from the main OpenClaw gateway that powers the admin agent (SaaSClaw itself). The two gateways are independent processes with different ports, configs, and state directories.

## Why Two Gateways?

| | Main Gateway | Wizard Gateway |
|---|---|---|
| **Port** | :18789 | :18790 |
| **Purpose** | Admin agent (SaaSClaw), Telegram | Wizard chat completions only |
| **Auth** | API keys + Telegram | None (loopback-only) |
| **Plugins** | Full set (Telegram, browser, etc.) | Minimal (5 plugins) |
| **State dir** | `~/.openclaw/` | `~/.openclaw-wizard-state/` |
| **Config** | `~/.openclaw/openclaw.json` | `~/.openclaw/openclaw-wizard.json` |
| **Systemd** | `openclaw.service` | `openclaw-wizard.service` |

**Key reason**: Restarting the wizard gateway (e.g., to add models or change config) must **not** break the admin agent's Telegram session. Keeping them separate means wizard config changes are safe to make at any time.

## Architecture

```
Browser (wizard chat)
    ↓ HTTP POST /studio/wizard/send
Django app (studio/views/wizard.py)
    ↓ HTTP POST /v1/chat/completions
OpenClaw Wizard Gateway (:18790, loopback)
    ↓ HTTP POST
LLM Provider (Z.AI, OpenAI, etc.)
```

The wizard gateway runs on localhost only (`bind: loopback`). It is never exposed to the internet directly. The Django app is the only client.

## Installation

### 1. Install OpenClaw

```bash
npm install -g openclaw
```

### 2. Create Wizard Config

Save to `~/.openclaw/openclaw-wizard.json`:

```json
{
  "gateway": {
    "mode": "local",
    "auth": {
      "mode": "none"
    },
    "port": 18790,
    "bind": "loopback",
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    }
  },
  "plugins": {
    "entries": {
      "telegram": { "enabled": false }
    },
    "allow": [
      "browser", "canvas", "memory-core", "openai", "duckduckgo", "file-transfer"
    ],
    "deny": [
      "telegram", "phone-control", "talk-voice", "codex", "device-pair"
    ]
  },
  "tools": {
    "profile": "coding",
    "web": { "search": { "enabled": false } },
    "elevated": { "enabled": false }
  },
  "agents": {
    "defaults": {
      "models": {
        "zai/*": {},
        "openai/*": {}
      }
    }
  }
}
```

Key points:
- `auth.mode: "none"` — no auth needed since it's loopback-only
- `bind: "loopback"` — only listens on 127.0.0.1
- `chatCompletions.enabled: true` — the wizard only needs chat completions
- Telegram and other interactive plugins are **disabled** — the wizard is a backend service
- Model allowlists use wildcards (`zai/*`, `openai/*`) so new models are available immediately

### 3. Create State Directory

```bash
mkdir -p ~/.openclaw-wizard-state
```

OpenClaw stores session data, conversation history, and agent state here. Keeping it separate from the main gateway's state directory prevents cross-contamination.

### 4. Create Systemd Service

Save to `/etc/systemd/system/openclaw-wizard.service`:

```ini
[Unit]
Description=OpenClaw Wizard Gateway (chat completions only)
After=network.target docker.service

[Service]
Type=simple
Environment=OPENCLAW_CONFIG_PATH=/home/nmoore/.openclaw/openclaw-wizard.json
Environment=OPENCLAW_STATE_DIR=/home/nmoore/.openclaw-wizard-state
Environment=HOME=/home/nmoore
ExecStart=/usr/bin/node /home/nmoore/.npm-global/lib/node_modules/openclaw/dist/index.js gateway --port 18790 --allow-unconfigured
Restart=always
RestartSec=5
User=nmoore

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable openclaw-wizard
sudo systemctl start openclaw-wizard
```

The `--allow-unconfigured` flag allows the gateway to start even without a fully configured admin agent (it's only serving API requests, not hosting sessions).

### 5. Connect Django to the Wizard

In Django settings (`config/settings.py`):

```python
STUDIO_LOCAL_URL = os.environ.get('STUDIO_LOCAL_URL', 'http://127.0.0.1:18790/v1')
```

In the wizard view (`studio/views/wizard.py`), when `project.require_gateway` is set, the view forces provider to `local` and sets the gateway URL:

```python
if project.require_gateway:
    gateway_url = getattr(settings, 'LLM_GATEWAY_URL', 'http://127.0.0.1:18790/v1')
    provider = 'local'
    model = gateway_model
    os.environ['STUDIO_LOCAL_URL'] = gateway_url
```

### 6. Environment Variables

| Variable | Where | Purpose |
|---|---|---|
| `OPENCLAW_CONFIG_PATH` | systemd service | Points to wizard config JSON |
| `OPENCLAW_STATE_DIR` | systemd service | Separate state directory |
| `STUDIO_LOCAL_URL` | Django app | URL of the wizard gateway |

## Adding LLM Providers

To add a new provider, edit `~/.openclaw/openclaw-wizard.json` and add it under `models.providers`. Then restart:

```bash
sudo systemctl restart openclaw-wizard
```

Since this only affects the wizard gateway, the admin agent on :18789 stays up.

## What the Wizard Gateway Does NOT Do

- **No Telegram** — it's a backend service, not an interactive agent
- **No browser automation** — though the browser plugin is loaded for potential future use
- **No web search** — disabled to reduce attack surface and cost
- **No elevated commands** — the wizard's own tool execution (Docker sandbox) handles shell commands
- **No auth** — loopback-only access means the Django app is the only possible client

## Troubleshooting

**Gateway won't start:**
```bash
sudo journalctl -u openclaw-wizard --since "5 min ago"
```

**Check if it's running:**
```bash
curl -s http://127.0.0.1:18790/v1/models | head -20
```

**Django can't reach it:**
```bash
# Verify loopback binding
ss -tlnp | grep 18790
# Should show 127.0.0.1:18790 only
```

**After config changes**, always restart:
```bash
sudo systemctl restart openclaw-wizard
```

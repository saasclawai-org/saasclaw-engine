# Architecture

## Overview

SaaSClaw Engine is a multi-tenant AI application builder. Users create projects, an AI coding agent writes/modifies code, and the deploy pipeline builds and serves the result. The system is designed for concurrent multi-user operation on a single VPS.

```
┌─────────────┐     ┌──────────────────┐     ┌──────────┐
│  SaaSClaw   │────▶│  SaaSClaw Engine  │────▶│  ZAI /   │
│  Web App    │     │  (Django + Celery) │     │  OpenAI  │
│  (React)    │     │                    │     │  Anthropic│
└─────────────┘     └──────────────────┘     └──────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌──────────┐ ┌────────────┐
         │ Celery  │ │  Deploy  │ │  Docker    │
         │ Worker  │ │  Worker  │ │  Sandbox   │
         │ (4x)    │ │  (1x)    │ │  (per-cmd) │
         └─────────┘ └──────────┘ └────────────┘
              │            │
              ▼            ▼
         ┌──────────────────────┐
         │  /srv/saasclaw/      │
         │  projects/<name>/    │
         │    repo/   runtime/  │
         └──────────────────────┘
```

## LLM Gateway (Dual-Gateway Setup)

SaaSClaw uses two OpenClaw gateway instances to separate admin infrastructure from user-facing wizard work.

### Main Gateway (:18789)

The primary gateway handles admin operations (Telegram bot, control UI, exec tools). It should not be restarted unnecessarily — restarts drop active Telegram connections.

### Wizard Gateway (:18790)

A dedicated, minimal gateway instance for wizard LLM requests only. This allows the wizard infrastructure to be reconfigured or restarted without affecting admin sessions.

```
Wizard ──▶ Engine (celery worker) ──▶ Wizard Gateway (:18790) ──▶ ZAI API
                │
                │ runs tools via Docker sandbox
                ▼
           Project workspace (isolated)
```

**Wizard gateway config:** `/home/nmoore/.openclaw/openclaw-wizard.json`
**Wizard gateway state:** `/home/nmoore/.openclaw-wizard-state/`
**Systemd service:** `openclaw-wizard.service`

Key differences from the main gateway:
- No Telegram plugin (no conflict with main gateway)
- Only essential plugins: browser, canvas, file-transfer, memory-core, openai
- No auth required (loopback-only, network-isolated)
- Separate state directory prevents cross-contamination

**Environment variables:**
```
STUDIO_LOCAL_URL=http://127.0.0.1:18790/v1
```

### LLM Provider Config

The wizard gateway uses `OPENCLAW_CONFIG_PATH` to load a dedicated config. Model allowlists use wildcards (`zai/*`, `openai/*`) so any provider model is automatically available without manual allowlisting.

## Agent Sandbox

All shell commands executed by the AI agent run inside ephemeral Docker containers for per-project isolation.

### How It Works

1. Agent requests a tool call (e.g. `run_command("npm run build")`)
2. Engine spawns a Docker container with only the project workspace mounted
3. Command executes inside the container with no network access
4. Container is destroyed after execution (`--rm`)

### Sandbox Image

`saasclaw-sandbox:latest` — Debian-based image with Node.js 22, Python 3, Git, GCC, and npm.

**Dockerfile:** `/srv/saasclaw/engine/Dockerfile.sandbox`

### Container Restrictions

| Restriction | Setting | Purpose |
|------------|---------|---------|
| Filesystem | Own rootfs + workspace mount only | Prevents reading other projects |
| Network | `--network none` | Prevents data exfiltration |
| Memory | 512MB limit | Prevents resource exhaustion |
| CPU | 1 CPU limit | Prevents resource exhaustion |
| PIDs | 100 max | Prevents fork bombs |
| User | 1001:1001 (unprivileged) | No root inside container |
| Tmpfs | `/tmp` (512MB), `/home/sandbox` (64MB) | Writable scratch space |
| Lifecycle | `--rm` (ephemeral) | No lingering containers |

### File Tool Validation

File tools (`read_file`, `write_file`, `replace_in_file`, `list_files`, `apply_patch`) validate paths with `_safe_path()`:

- Resolves symlinks via `os.path.realpath()` to prevent escape
- Ensures resolved path stays within the project workspace
- Rejects any path traversal (`../`) outside the workspace

### Web Fetch/Web Search Restrictions

`web_fetch()` only allows requests to known documentation hosts (MDN, Python docs, Django, React, npmjs, StackOverflow, GitHub, CDNJS). This prevents the agent from exfiltrating data to arbitrary endpoints.

### Implementation

All sandbox logic is in `/srv/saasclaw/engine/saasclaw_engine/agent/tools.py`:

- `_run_in_sandbox()` — Docker container execution wrapper
- `_safe_path()` — Path validation for file tools
- `WEB_FETCH_ALLOWED_HOSTS` — URL allowlist for web_fetch
- `SANDBOX_ENABLED` — Toggle to disable sandboxing (falls back to host)

## Concurrency & Scaling

| Component | Concurrency | Role |
|----------|------------|------|
| saasclaw-web | Multi-process (gunicorn) | Django web app |
| saasclaw-worker | 4 (prefork) | Wizard agent loops |
| saasclaw-deploy-worker | 1 | Sequential deploys |
| saasclaw-beat | 1 | Periodic tasks |
| Wizard gateway | Unbounded | LLM API proxy |
| Docker sandbox | Unbounded (ephemeral) | Per-command isolation |

**Current capacity:** 4 concurrent wizard sessions. Deploys queue sequentially.

**Scaling considerations:**
- Increase `saasclaw-worker` concurrency for more parallel wizard sessions
- Add a second deploy worker for parallel deploys
- Monitor RAM — each sandbox container uses up to 512MB, each Celery worker uses ~200MB
- LLM provider rate limits may bottleneck concurrent sessions

## Project Directory Structure

```
/srv/saasclaw/projects/<project-slug>/
├── repo/              # Git workspace (agent reads/writes here)
│   ├── src/
│   ├── package.json / requirements.txt / *.csproj
│   └── .git/
├── runtime/
│   ├── preview/
│   │   ├── .env      # Environment variables
│   │   └── web/      # Built output served by nginx
│   └── production/
│       ├── .env
│       └── web/
└── logs/
    └── deploy-*.log
```

## User Isolation

Each project is isolated at multiple layers:

1. **Docker sandbox** — Shell commands can only access the project's own `repo/` directory
2. **Path validation** — File tools reject access outside the workspace
3. **Network isolation** — Sandbox containers have no network access
4. **User permissions** — Projects owned by `saasclaw:saasclaw`, setgid ensures group-writable access

## Deploy Pipeline

The deploy pipeline (in `saasclaw_engine/deployments/service.py`) runs outside the sandbox as it needs access to:
- systemd (to create/restart services)
- nginx (to configure virtual hosts)
- The broader `/srv/saasclaw/` tree

Deploy steps:
1. `git add -A && git commit` (as `saasclaw` user with deploy SSH key)
2. Build (`npm run build` / `python manage.py collectstatic` / `dotnet publish`)
3. Copy built output to `runtime/<env>/web/`
4. Configure systemd service and nginx vhost
5. Health check (wait for HTTP 200)
6. Mark deployment as successful

## Security Notes

- **Docker group membership** — The `saasclaw` user is in the `docker` group, which is effectively root-equivalent on the host. This is acceptable for a single-admin VPS but would need rethinking for shared infrastructure.
- **No inter-project access** — A wizard session for project A cannot read project B's files, environment variables, or database.
- **Blocked commands** — `sudo`, `rm -rf /`, `curl`, `wget`, `nc`, `ssh`, `scp` are blocked at the tool level as an additional safety net.
- **Prompt injection guard** — Multimodal content (image uploads) is scanned for prompt injection attempts before processing.

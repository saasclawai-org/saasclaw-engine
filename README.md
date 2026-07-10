# SaaSClaw Engine

The open-source AI-powered application build, deploy, and agent engine for [SaaSClaw](https://saasclaw.ai).

[![AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

SaaSClaw Engine is the backend that powers [SaaSClaw](https://saasclaw.ai). It provides the deploy pipeline, AI coding agent system, GitHub integration, and all data models — everything you need to build your own AI app builder on top of.

## What It Does

- **AI Agent** — Run LLM-powered agents via OpenClaw Gateway with tools for file I/O, shell commands, git, web search, and todo tracking
- **Deploy Pipeline** — Build and deploy projects to preview and production environments with automatic nginx config, SSL, health checks, and rollback
- **GitHub Integration** — Clone, commit, and push to user repos via a GitHub App (JWT auth + installation tokens)
- **Task Queue** — Async Celery workers for long-running deploy jobs
- **API Key Management** — Encrypted storage for user-provided LLM provider keys
- **Risk Tier Classification** — Automatic Low/Medium/High/Critical risk assignment based on data sensitivity
- **Secret Scanning** — Deploy pipeline detects AWS keys, GitHub tokens, private keys, and other secrets in committed code
- **Dependency Scanning** — Automated vulnerability scanning (`npm audit`, `pip check`) during deploy
- **Decommissioning** — Safe project decommissioning with systemd cleanup, nginx removal, and audit logging
- **Per-Project Databases** — Auto-provisioned PostgreSQL databases for each deployed project
- **Form API** — Static sites can submit form data via a secure API endpoint (no backend needed)

## Install

```bash
pip install saasclaw-engine
```

Requires: Python 3.11+, Django 5.0+, PostgreSQL, Redis, Celery

## Quick Start

### 1. Create a Django project

```bash
django-admin startproject myapp
cd myapp
```

### 2. Install dependencies

```bash
# The engine
pip install saasclaw-engine

# PostgreSQL adapter + Celery broker
pip install 'psycopg[binary]' 'celery[redis]' redis
```

### 3. Add to INSTALLED_APPS

```python
# settings.py

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Engine
    'saasclaw_engine.projects',
    'saasclaw_engine.deployments',
    'saasclaw_engine.integrations',
    'saasclaw_engine.agents',
    'saasclaw_engine.studio_models',
    # Your UI apps
    'myapp',
]
```

### 4. Configure settings

```python
# settings.py

DATABASE_URL = 'postgresql://user:pass@localhost/myapp'

CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'

# Filesystem paths (adjust to your server)
GIT_ROOT = '/srv/myapp/git'           # Bare git repos
PROJECT_ROOT = '/srv/myapp/projects'   # Deployed project checkouts
LOG_ROOT = '/srv/myapp/logs'          # Deploy logs

# SSL — the deploy pipeline expects Let's Encrypt certs at these paths:
#   /etc/letsencrypt/live/yourdomain.com/fullchain.pem
#   /etc/letsencrypt/live/yourdomain.com/privkey.pem
#   /etc/letsencrypt/live/preview.yourdomain.com/fullchain.pem
#   /etc/letsencrypt/live/preview.yourdomain.com/privkey.pem
# See DNS & SSL section below.

# GitHub App (optional — see GitHub App section below)
GITHUB_APP_ID = ''
GITHUB_APP_PRIVATE_KEY_PATH = ''
GITHUB_WEBHOOK_SECRET = ''

STATIC_URL = '/static/'
STATIC_ROOT = '/var/www/myapp/static'
```

### 5. Create directories and run migrations

```bash
# Create filesystem structure
sudo mkdir -p /srv/myapp/git /srv/myapp/projects /srv/myapp/logs

# Migrate
python manage.py migrate

# Create admin user
python manage.py createsuperuser
```

### 6. Wire the URLs

```python
# urls.py

from django.urls import path
from saasclaw_engine.integrations.views import github_setup, github_webhook

urlpatterns = [
    path('admin/', admin.site.urls),
    path('github/webhook/', github_webhook, name='github_webhook'),
    path('github/setup/', github_setup, name='github_setup'),
    # Your app's URLs
]
```

### 7. Run it

```bash
# Web server (use gevent workers for SSE streaming)
gunicorn myapp.wsgi:application \
    --bind 127.0.0.1:8010 \
    --worker-class gevent --workers 4 \
    --timeout 600

# Celery worker (async deploys)
celery -A myapp worker -l info

# Celery beat (periodic tasks)
celery -A myapp beat -l info
```

> **Important:** Use `--worker-class gevent` (not gthread) if your app uses SSE streaming. The gthread worker buffers streaming responses entirely before sending them. Also set `conn_max_age=0` when using gevent — greenlets cannot share DB connections.

---

## AI Wizard — OpenClaw Gateway

The wizard (AI chat interface where users describe what they want built) requires an [OpenClaw](https://github.com/openclaw/openclaw) gateway for LLM routing.

### Install OpenClaw

```bash
npm install -g openclaw
```

### Quick Setup

1. Create a wizard config:
   ```bash
   mkdir -p ~/.openclaw ~/.openclaw-wizard-state
   cat > ~/.openclaw/openclaw-wizard.json << 'EOF'
   {
     "gateway": {
       "mode": "local",
       "port": 18790,
       "bind": "loopback",
       "auth": { "mode": "none" }
     }
   }
   EOF
   ```

2. Start the gateway (or create a systemd service — see [docs/WIZARD-GATEWAY.md](docs/WIZARD-GATEWAY.md)):
   ```bash
   OPENCLAW_CONFIG_PATH=~/.openclaw/openclaw-wizard.json \
     OPENCLAW_STATE_DIR=~/.openclaw-wizard-state \
     openclaw gateway --port 18790
   ```

3. Verify: `curl -s http://127.0.0.1:18790/v1/models`

The Django app connects to the wizard at `http://127.0.0.1:18790/v1` by default (configurable via `STUDIO_LOCAL_URL` setting).

> **Full guide:** See [docs/WIZARD-GATEWAY.md](docs/WIZARD-GATEWAY.md) for systemd service setup, multi-provider config, LLM Gateway mode, and troubleshooting.

---

## Production Deployment

### Systemd Services

**`/etc/systemd/system/saasclaw-web.service`:**

```ini
[Unit]
Description=SaaSClaw Gunicorn
After=network.target postgresql.service redis-server.service

[Service]
User=saasclaw
Group=saasclaw
WorkingDirectory=/srv/saasclaw/app
EnvironmentFile=/srv/saasclaw/app/.env
ExecStart=/srv/saasclaw/app/.venv/bin/gunicorn config.wsgi:application \
    --bind 127.0.0.1:8010 \
    --worker-class gevent --workers 4 \
    --timeout 600 \
    --access-logfile /srv/saasclaw/logs/gunicorn-access.log \
    --error-logfile /srv/saasclaw/logs/gunicorn-error.log
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/saasclaw-worker.service`:**

```ini
[Unit]
Description=SaaSClaw Celery Worker
After=network.target redis-server.service

[Service]
Type=simple
User=saasclaw
Group=saasclaw
WorkingDirectory=/srv/saasclaw/app
EnvironmentFile=/srv/saasclaw/app/.env
Environment=HOME=/srv/saasclaw
Environment=NPM_CONFIG_CACHE=/srv/saasclaw/.npm
ExecStart=/srv/saasclaw/app/.venv/bin/celery -A config worker -l info
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable saasclaw-web saasclaw-worker
sudo systemctl start saasclaw-web saasclaw-worker
```

### Nginx Reverse Proxy

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name saasclaw.ai;

    ssl_certificate     /etc/letsencrypt/live/saasclaw.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/saasclaw.ai/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 25m;

    location /static/ {
        alias /srv/saasclaw/app/static/;
        expires 30d;
    }

    location /docs/ {
        alias /srv/saasclaw/app/docs/;
    }

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }
}
```

---

## DNS & SSL

The deploy pipeline automatically generates nginx configs for each project. It needs DNS records pointing to your server and SSL certificates for every domain it serves.

### DNS Records

Assuming your server IP is `203.0.113.50` and your domain is `example.com`:

| Type | Name | Value | Purpose |
|------|------|-------|---------|
| A | `@` | `203.0.113.50` | Main app (`example.com`) |
| A | `preview` | `203.0.113.50` | Preview subdomain |
| A | `*` | `203.0.113.50` | Wildcard — catches `*.example.com` and `*.preview.example.com` |

**If using Cloudflare**, set all records to **DNS only** (grey cloud). The engine generates its own nginx SSL config — Cloudflare's proxy would conflict.

> **Wildcard A records** aren't supported by all DNS providers. If yours doesn't support them, add individual A records for each project slug.

### SSL Certificates (Let's Encrypt)

```bash
# Production wildcard
sudo certbot certonly --manual --preferred-challenges dns \
  -d example.com -d '*.example.com' \
  --agree-tos --email you@example.com

# Preview wildcard
sudo certbot certonly --manual --preferred-challenges dns \
  -d preview.example.com -d '*.preview.example.com' \
  --agree-tos --email you@example.com
```

**Using Cloudflare DNS?** certbot's Cloudflare plugin automates the DNS challenge:

```bash
pip install certbot-dns-cloudflare

sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/cloudflare/credentials.ini \
  -d example.com -d '*.example.com'
```

### Auto-renewal

```bash
sudo crontab -e
# Add:
0 3 * * * certbot renew --quiet --deploy-hook "systemctl reload nginx"
```

---

## GitHub App Integration

The engine connects to **users' own GitHub repos** via a GitHub App. Each user installs the app on their own account or org.

### How It Works

1. **Instance owner** creates a GitHub App (one-time setup)
2. **End users** install the app on their GitHub accounts
3. GitHub fires an `installation` webhook → engine links the installation to that user
4. Users pick from **their own installations** when creating projects
5. The agent clones, commits, and pushes using installation-scoped tokens

> Users bring their own repos. The instance owner never has access to user repos.

### Setup

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Set:
   - **Homepage URL**: your app URL
   - **Webhook URL**: `https://example.com/github/webhook/`
   - **Webhook secret**: generate a random string
3. Under **Repository permissions**: Contents = Read & write, Metadata = Read-only
4. Under **Subscribe to events**: check `installation` and `installation_repositories`
5. Create the app, then **Generate a new private key** (.pem file)
6. Store credentials and configure in Django settings:

```python
GITHUB_APP_ID = '123456'
GITHUB_APP_PRIVATE_KEY_PATH = '/etc/myapp/secrets/github-app.pem'
GITHUB_WEBHOOK_SECRET = 'whsec_your-random-secret-here'
```

---

## How Projects Store Data

Every deployed project gets a dedicated PostgreSQL database, auto-provisioned on deploy.

| Environment | Database Name | Role |
|-------------|-------------|------|
| Preview | `saasclaw_{slug}` | `sc_{slug}` |
| Production | `saasclaw_{slug}_production` | `sc_{slug}_production` |

Connection details are injected as environment variables (`DATABASE_URL`, `POSTGRES_*`, `ConnectionStrings__DefaultConnection` for .NET).

### Form API for Static Sites

Static sites (HTML, React, Vue, Svelte, Hugo) can submit form data via a secure API — no backend needed in the project.

**Endpoint:** `POST /api/forms/{project-slug}/`

Security: per-project API key (`X-Form-Key` header), Redis rate limiting (10/min per IP), origin validation, honeypot anti-spam.

See the [SaaSClaw app README](https://github.com/saasclawai-org/saasclaw) for full Form API documentation.

---

## Engine API

### Deploy Pipeline

```python
from saasclaw_engine.deployments.service import deploy_preview, deploy_production

deploy_preview(project, user, log_file)
deploy_production(project, environment, user, log_file)
```

### Decommissioning

```python
from saasclaw_engine.deployments.service import decommission_project

decommission_project(project_slug, project_name)
```

### Agent System

The simplest way to use the agent is through the wizard gateway's OpenAI-compatible API. Once you have the engine running with an OpenClaw gateway (see [AI Wizard](#ai-wizard--openclaw-gateway) above), any OpenAI-compatible client works:

#### Quick Example: Create and edit a project via LLM

```python
"""Create a project and have the AI agent build it."""
import requests, json, time

# --- 1. Create a project via Django ORM ---
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myapp.settings')
django.setup()

from saasclaw_engine.projects.models import Project
from saasclaw_engine.studio_models.models import Workspace, AgentSession
from django.contrib.auth.models import User

user = User.objects.get(username='me')
project = Project.objects.create(
    name='My AI App',
    slug='my-ai-app',
    owner=user,
    framework='vite',  # vite, nextjs, django, flask, htmx, hugo, dotnet
)
workspace = Workspace.objects.create(
    project=project,
    local_path=f'/srv/myapp/projects/{project.slug}/repo',
    is_active=True,
    user=user,
)

# --- 2. Send the build instruction to the agent ---
GATEWAY_URL = 'http://127.0.0.1:18790/v1/chat/completions'
SESSION_KEY = f'wizard-{project.slug}'

resp = requests.post(GATEWAY_URL, json={
    'model': 'openclaw',  # routes to the agent loop with tools
    'sessionKey': SESSION_KEY,
    'stream': False,
    'messages': [{
        'role': 'user',
        'content': 'Build a todo list app with add, complete, and delete. Use React + TypeScript. Make it look clean with a dark theme.',
    }],
}, timeout=300)
result = resp.json()
assistant_message = result['choices'][0]['message']['content']
print('Agent:', assistant_message)

# --- 3. Iterate: send a follow-up edit ---
resp = requests.post(GATEWAY_URL, json={
    'model': 'openclaw',
    'sessionKey': SESSION_KEY,  # same key = same conversation
    'stream': False,
    'messages': [{
        'role': 'user',
        'content': 'Add local storage persistence so todos survive page refresh.',
    }],
}, timeout=300)
print('Follow-up:', resp.json()['choices'][0]['message']['content'])

# --- 4. Deploy ---
from saasclaw_engine.deployments.service import deploy_preview
deployment = deploy_preview(project, triggered_by=user)
print(f'Deploy {deployment.status}: https://{project.slug}.preview.example.com/')
```

#### Using the agent runner directly (Python only)

```python
from saasclaw_engine.agent.runner import run_agent

messages = run_agent(
    workspace_path='/srv/myapp/projects/my-ai-app/repo',
    project_name='My AI App',
    conversation=[],  # empty = new conversation
    user_message='Build a Django REST API with user auth',
    provider='openai',          # openai, anthropic, zai, or local
    model='gpt-5.4-mini',       # any model the provider supports
    user=user,
    project_context='This is a fresh Vite + React + TypeScript project.',
)
# messages = list of {role, content, tool_call} dicts from the agent
for msg in messages:
    print(f'[{msg["role"]}] {msg["content"][:200]}')
```

**Providers:** `openai` (GPT-5.5, GPT-5.4, etc.), `anthropic` (Claude family), `zai` (GLM family), `local` (your own gateway)

> **BYO keys:** Users add their own provider API keys via the encrypted `ProviderKey` model. The agent uses the user's key — no markup, no middleman.

### GitHub Integration

```python
from saasclaw_engine.integrations.github import (
    clone_or_update_repo, commit_and_push_repo, get_installation_token,
)

token = get_installation_token(installation_id)
clone_or_update_repo(project, token)
commit_and_push_repo(project, message="Add new feature", token=token)
```

---

## PII Protection

Every message sent to an LLM passes through **PII Guard**, a Presidio-based microservice on `localhost:8900`.

**Detection patterns:** SSNs, credit cards, phone numbers, emails, addresses, bank accounts, DOB, passports, driver's licenses, salary, DB connection strings, AWS keys, IP addresses.

**Redaction:** Detected values are replaced with synthetic placeholders (`{{SSN}}`, `{{EMAIL}}`, etc.) before reaching the LLM.

**Fallback:** If the service is unreachable, identical built-in regex patterns are used — zero downtime.

### LLM Gateway Mode

For projects requiring data never leave your infrastructure, enable **LLM Gateway mode** per-project. This forces all agent requests through a local LLM endpoint (vLLM, Ollama, LM Studio) and blocks cloud providers.

### Prompt Injection Defense

All user input is scanned using the [sunglasses](https://github.com/sunglasses-dev/sunglasses) library (1094 patterns, 65 attack categories, 23 languages). Dual-layer defense scans at both the wizard endpoint and the agent runner.

---

## System Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.11+ | |
| PostgreSQL | 14+ | Required |
| Redis | 6+ | Celery broker |
| Nginx | 1.18+ | Per-project config generation |
| Certbot | 1.20+ | SSL certificate management |
| Node.js | 18+ | Via fnm (auto-detected per project) |
| OpenClaw | latest | `npm install -g openclaw` (required for AI wizard) |
| .NET SDK | 9+ | Optional — auto-installed on demand for .NET projects |

## Supported Frameworks

Vite (React/Vue/Svelte), Next.js (SSR), Django, Flask, FastAPI, HTMX, Hugo, .NET/C#, static HTML

## Project Components

| Package | Description |
|---------|-------------|
| `saasclaw_engine.agent` | Agent runner and tools (file I/O, bash, git, web, todos) |
| `saasclaw_engine.deployments` | Models, deploy pipeline with secret/dependency scanning, nginx config generation, decommissioning |
| `saasclaw_engine.integrations` | GitHub App auth, per-user installation scoping, webhook handling |
| `saasclaw_engine.agents` | Celery task models and async task execution |
| `saasclaw_engine.projects` | Project model with framework, runtime, risk tier, and config fields |
| `saasclaw_engine.studio_models` | AgentSession, ProviderKey, Workspace, Todo, TokenUsage models |
| `saasclaw_engine.help_search` | RAG-based help search using ChromaDB |

## Why Self-Host?

SaaSClaw Engine is the open-source alternative to closed AI app builders. When you self-host, there are no per-seat licenses, no credit systems, and no token markups — you bring your own LLM API keys and pay your provider directly.

| Feature | SaaSClaw (self-hosted) | Bolt.new | Lovable | v0 (Vercel) |
|---|---|---|---|---|
| **License** | AGPL-3.0 (open source) | Proprietary | Proprietary | Proprietary |
| **Self-hostable** | ✅ Your server, your rules | ❌ | ❌ | ❌ |
| **Bring your own API keys** | ✅ Use any provider | ❌ | ❌ | ❌ |
| **LLM cost** | Your provider's raw cost (no markup) | Token packages | Credit system | Credit system |
| **Project limit** | Unlimited (your hardware) | Plan-based | Plan-based | Plan-based |
| **Data residency** | You control it | Their cloud | Their cloud | Their cloud |
| **Code ownership** | Full — it's your Git repo | Export | GitHub sync | Export |
| **Vendor lock-in** | None | High | High | High (Vercel) |
| **Multi-framework** | Django, React, Next.js, Svelte, .NET, Hugo | React, Svelte | React/Next.js | React/Next.js |
| **Agent loop** | OpenClaw (open source) | Closed | Closed | Closed |
| **Deploy to own server** | ✅ Built-in | ❌ | ❌ | ❌ (Vercel only) |
| **Built-in database** | PostgreSQL per project | ✅ | Supabase | ❌ |
| **Form API for static sites** | ✅ | ❌ | ❌ | ❌ |
| **PII redaction** | ✅ Presidio + regex fallback | ❌ | ❌ | ❌ |
| **Prompt injection defense** | ✅ 1094 patterns, 23 languages | ❌ | ❌ | ❌ |

With SaaSClaw self-hosted, your only costs are the infrastructure you already run and whatever LLM API fees you already pay. No subscriptions, no credits, no token packages to purchase.

---

## Testing

576 tests across 16 test files.

```bash
python -m pytest           # run all
python -m pytest -v        # verbose
python -m pytest -k "form" # filter by name
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

## Links

- [SaaSClaw](https://saasclaw.ai) — The production platform built on this engine
- [Issues](https://github.com/saasclawai-org/saasclaw-engine/issues) — Bug reports and feature requests

# SaaSClaw Engine

The open-source AI-powered application build, deploy, and agent engine for [SaaSClaw](https://saasclaw.ai).

[![AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

SaaSClaw Engine is the backend that powers [SaaSClaw](https://saasclaw.ai). It provides the deploy pipeline, AI coding agent system, GitHub integration, and all data models — everything you need to build your own AI app builder on top of.

## What It Does

- **AI Agent** — Run LLM-powered agents (via Pi RPC or direct API) with tools for file I/O, shell commands, git, web search, and todo tracking
- **Deploy Pipeline** — Build and deploy projects to preview and production environments with automatic nginx config, SSL, health checks, and rollback
- **GitHub Integration** — Clone, commit, and push to user repos via a GitHub App (JWT auth + installation tokens)
- **Task Queue** — Async Celery workers for long-running deploy jobs
- **API Key Management** — Encrypted storage for user-provided LLM provider keys
- **Risk Tier Classification** — Automatic Low/Medium/High/Critical risk assignment based on data sensitivity
- **Secret Scanning** — Deploy pipeline detects AWS keys, GitHub tokens, private keys, and other secrets in committed code
- **Dependency Scanning** — Automated vulnerability scanning (`npm audit`, `pip check`) during deploy
- **Decommissioning** — Safe project decommissioning with systemd cleanup, nginx removal, and audit logging
- **Staging Support** — Configurable preview domains for staging isolation (`PREVIEW_BASE_DOMAIN`)

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
# Web
gunicorn myapp.wsgi:application --bind 127.0.0.1:8000 --workers 4

# Celery worker (async deploys)
celery -A myapp worker -l info

# Celery beat (periodic tasks)
celery -A myapp beat -l info
```

---

## Production Deployment

### Systemd Services

Create two service files for Gunicorn (web) and Celery (worker). These use your app's virtual environment and environment file.

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
    --bind 127.0.0.1:8000 --workers 2 --threads 4 \
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

> **Important:** After code changes, use `sudo systemctl restart saasclaw-web saasclaw-worker`. For module-level changes (like new Django apps or migrations), do a full stop/start: `sudo systemctl stop saasclaw-web saasclaw-worker && sudo systemctl start saasclaw-web saasclaw-worker`.

### Nginx Reverse Proxy

The web service listens on `127.0.0.1:8000`. Configure nginx to proxy to it:

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name saasclaw.ai;

    ssl_certificate     /etc/letsencrypt/live/saasclaw.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/saasclaw.ai/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    # Static files (serve directly)
    location /static/ {
        alias /srv/saasclaw/app/static/;
        expires 30d;
    }

    # Docs
    location /docs/ {
        alias /srv/saasclaw/app/docs/;
    }

    # All other requests → Gunicorn
    location / {
        proxy_pass http://127.0.0.1:8000;
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

### Required Directories

```bash
sudo mkdir -p /srv/saasclaw/{app,engine,logs}
sudo useradd -r -d /srv/saasclaw -s /usr/sbin/nologin saasclaw
sudo chown -R saasclaw:saasclaw /srv/saasclaw
```

### Environment File

Create `/srv/saasclaw/app/.env` with your settings (see [Configuration](#configuration) above). Key variables:

```env
SECRET_KEY=your-django-secret-key
DATABASE_URL=postgres://user:pass@localhost/saasclaw
CELERY_BROKER_URL=redis://localhost:6379/0
REDIS_URL=redis://localhost:6379/0
STUDIO_MODEL=glm-5.2
ALLOWED_HOSTS=saasclaw.ai,app.saasclaw.ai
```

### Pi Extension (PII Guard)

The PII Guard Pi extension (`extensions/pii-guard.ts`) calls the same PII Guard service over HTTP, with regex fallback:

```bash
sudo mkdir -p ~saasclaw/.pi/agent/extensions
sudo cp /srv/saasclaw/engine/extensions/pii-guard.ts ~saasclaw/.pi/agent/extensions/
sudo chown -R saasclaw:saasclaw ~saasclaw/.pi
```

The service must be running (`sudo systemctl start pii-guard`). If it's not, the extension falls back to built-in regex automatically.

---

## DNS & SSL

The deploy pipeline automatically generates nginx configs for each project. It needs DNS records pointing to your server and SSL certificates for every domain it serves.

### DNS Records

Assuming your server IP is `203.0.113.50` and your domain is `example.com`, create these records at your DNS provider:

| Type | Name | Value | Purpose |
|------|------|-------|---------|
| A | `@` | `203.0.113.50` | Main app (`example.com`) |
| A | `preview` | `203.0.113.50` | Preview subdomain (`preview.example.com`) |
| A | `*` | `203.0.113.50` | Wildcard — catches `*.example.com` (production deploys) and `*.preview.example.com` (preview deploys) |

**If using Cloudflare**, set all records to **DNS only** (grey cloud, not orange proxy). The engine generates its own nginx SSL config using Let's Encrypt certs — Cloudflare's proxy would conflict.

> **Wildcard A records** aren't supported by all DNS providers. If yours doesn't support them, add individual A records for each project slug as you create them. The engine can't provision DNS — records must exist before deployment.

### SSL Certificates (Let's Encrypt)

The deploy pipeline looks for certs at these paths:

```
/etc/letsencrypt/live/example.com/fullchain.pem
/etc/letsencrypt/live/example.com/privkey.pem
/etc/letsencrypt/live/preview.example.com/fullchain.pem
/etc/letsencrypt/live/preview.example.com/privkey.pem
```

It also includes these Let's Encrypt files in every nginx config:

```
/etc/letsencrypt/options-ssl-nginx.conf
/etc/letsencrypt/ssl-dhparams.pem
```

You need **two wildcard certificates**. Request them with certbot:

```bash
# Production wildcard — covers example.com and *.example.com
sudo certbot certonly --manual --preferred-challenges dns \
  -d example.com -d '*.example.com' \
  --agree-tos --email you@example.com

# Preview wildcard — covers preview.example.com and *.preview.example.com
sudo certbot certonly --manual --preferred-challenges dns \
  -d preview.example.com -d '*.preview.example.com' \
  --agree-tos --email you@example.com
```

**Using Cloudflare DNS?** certbot's Cloudflare plugin can automate the DNS challenge:

```bash
# Install the plugin
pip install certbot-dns-cloudflare

# Create credentials file
sudo mkdir -p /etc/cloudflare
sudo tee /etc/cloudflare/credentials.ini << 'EOF'
dns_cloudflare_api_token = YOUR_API_TOKEN
EOF
sudo chmod 600 /etc/cloudflare/credentials.ini

# Request certs (non-interactive)
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/cloudflare/credentials.ini \
  -d example.com -d '*.example.com'

sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/cloudflare/credentials.ini \
  -d preview.example.com -d '*.preview.example.com'
```

### Auto-renewal

Add a cron job to renew certs before they expire:

```bash
sudo crontab -e
# Add this line:
0 3 * * * certbot renew --quiet --deploy-hook "systemctl reload nginx"
```

---

## GitHub App Integration

The engine can clone, commit, and push to users' GitHub repos via a [GitHub App](https://docs.github.com/en/developers/apps). Users install the app on their account/org, and the engine creates installation-scoped access tokens.

### How It Works

1. You create a GitHub App (once)
2. Users install it on their account or org
3. GitHub fires an `installation` webhook to your server
4. The engine records the installation
5. When the agent needs to work on a connected repo, it creates an installation access token (signed with your app's private key)
6. The agent clones, commits, and pushes using that token

### Step 1: Create the GitHub App

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Fill in:
   - **GitHub App name**: e.g. `MyApp Builder`
   - **Homepage URL**: your app's URL (e.g. `https://example.com`)
   - **Webhook URL**: `https://example.com/github/webhook/`
   - **Webhook secret**: generate a random string and save it
3. Under **Repository permissions**:
   - **Contents**: Read and write
   - **Metadata**: Read-only
4. Under **Subscribe to events**: check `installation`
5. Click **Create GitHub App**
6. On the app's settings page, click **Generate a new private key** and download the `.pem` file

### Step 2: Store the credentials

Copy the `.pem` file to your server:

```bash
sudo mkdir -p /etc/myapp/secrets
sudo cp ~/Downloads/myapp-builder.2024-01-01.private-key.pem /etc/myapp/secrets/github-app.pem
sudo chmod 600 /etc/myapp/secrets/github-app.pem
```

Add to your Django settings:

```python
GITHUB_APP_ID = '123456'                                    # From the GitHub App settings page
GITHUB_APP_PRIVATE_KEY_PATH = '/etc/myapp/secrets/github-app.pem'
GITHUB_WEBHOOK_SECRET = 'whsec_your-random-secret-here'      # What you generated in step 1
```

### Step 3: Verify the webhook URL works

Before users can install your app, GitHub needs to successfully deliver a `ping` webhook. Make sure:

- Your server is reachable at the webhook URL (`https://example.com/github/webhook/`)
- The URL is wired in your `urls.py` (see Quick Start step 6)
- Gunicorn is running and can accept POST requests

### Permissions Summary

| Permission | Access | Why |
|------------|--------|-----|
| Contents | Read & write | Clone repos, commit & push agent changes |
| Metadata | Read-only | Identify installations |

### How Users Connect Their Repos

1. User visits your app's GitHub setup page (e.g. `https://example.com/github/setup/`)
2. GitHub App installation flow opens (you redirect to GitHub's installation URL)
3. User chooses which repos to grant access to
4. GitHub redirects back to your app and fires an `installation` webhook
5. The engine records the installation (app ID, account, repo list)
6. When the user creates a project connected to a GitHub repo, the engine uses the installation token to clone and push

---

## System Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.11+ | |
| PostgreSQL | 14+ | Required (not SQLite) |
| Redis | 6+ | Celery broker |
| Nginx | 1.18+ | Per-project config generation |
| Certbot | 1.20+ | SSL certificate management |
| Node.js | 18+ | Via fnm (auto-detected per project for Vite/Next.js builds) |
| Let's Encrypt | | certs at `/etc/letsencrypt/live/` |
| `.NET` SDK | 9+ | Optional — auto-installed on demand for .NET projects |

## Engine API

### Deploy Pipeline

```python
from saasclaw_engine.deployments.service import deploy_preview, deploy_production

# Deploy a project's latest commit to preview
deploy_preview(project, user, log_file)

# Promote to production
deploy_production(project, environment, user, log_file)
```

### Decommissioning

```python
from saasclaw_engine.deployments.service import decommission_project

# Safely decommission a project — stops services, removes nginx, logs actions
decommission_project(project_slug, project_name)
```

### Agent System

```python
from saasclaw_engine.agent.pi_bridge import PiBridge
from saasclaw_engine.studio_models.models import AgentSession

session = AgentSession.objects.create(
    project=project,
    user=user,
    provider='zai',
    model='glm-5.2',
)

bridge = PiBridge(project=project, session=session)
result = bridge.run("Build me a Django REST API with user auth")
```

### GitHub Integration

```python
from saasclaw_engine.integrations.github import (
    clone_or_update_repo,
    commit_and_push_repo,
    get_installation_token,
)

# Get an installation access token for a user's GitHub App installation
token = get_installation_token(installation_id)

# Clone a user's repo into a worktree
clone_or_update_repo(project, token)

# Commit changes and push
commit_and_push_repo(project, message="Add new feature", token=token)
```

## Project Components

| Package | Description |
|---------|-------------|
| `saasclaw_engine.agent` | Pi Bridge (RPC agent), fallback runner, and agent tools (file I/O, bash, git, web, todos) |
| `saasclaw_engine.deployments` | Models (Project, Environment, Deployment, Domain, EnvVar), deploy pipeline with secret/dependency scanning, nginx config generation, project decommissioning |
| `saasclaw_engine.integrations` | GitHub App auth, repo clone/push, webhook handling |
| `saasclaw_engine.agents` | Celery task models and async task execution |
| `saasclaw_engine.projects` | Project model with framework, runtime, risk tier, and config fields; ProjectSubmission with AI disclosure tracking |
| `saasclaw_engine.studio_models` | AgentSession, ProviderKey, Workspace, Todo, TokenUsage models |
| `saasclaw_engine.help_search` | RAG-based help search using ChromaDB |

## How Projects Store Data

Every deployed project on SaaSClaw gets access to a dedicated PostgreSQL database on the local server. This happens automatically — no manual database setup required.

### Automatic Database Provisioning

When a project is deployed, the deploy pipeline calls `_ensure_postgres_database()` which:

1. Connects to the local PostgreSQL instance as `saasclaw_admin`
2. Creates a dedicated role and database if they don't exist
3. Injects connection details as environment variables into the project's `.env` file

Preview and production environments get **separate databases** — no shared state:

| Environment | Database Name | Role | Password |
|-------------|-------------|------|----------|
| Preview | `saasclaw_{slug}` | `sc_{slug}` | Auto-generated |
| Production | `saasclaw_{slug}_production` | `sc_{slug}_production` | Auto-generated |

### Environment Variables

Each runtime receives these env vars in its `.env` file:

| Variable | Description | Python | Node SSR | .NET |
|----------|-------------|--------|----------|------|
| `DATABASE_URL` | Standard connection string | ✅ | ✅ | ✅ |
| `POSTGRES_DB` | Database name | ✅ | ✅ | ✅ |
| `POSTGRES_USER` | Role name | ✅ | ✅ | ✅ |
| `POSTGRES_PASSWORD` | Role password | ✅ | ✅ | ✅ |
| `POSTGRES_HOST` | Host (default `127.0.0.1`) | ✅ | ✅ | ✅ |
| `POSTGRES_PORT` | Port (default `5432`) | ✅ | ✅ | ✅ |
| `ConnectionStrings__DefaultConnection` | .NET convention | — | — | ✅ |

User-defined environment variables (set in the studio UI) override defaults and persist across redeploys.

### Per-Runtime Details

**Python (Django, Flask, FastAPI):**
- Django projects get automatic `manage.py migrate` and admin user creation on deploy
- Flask/htmx starter templates include `flask-sqlalchemy` and `flask-migrate` pre-configured
- Templates use an app factory pattern that reads `DATABASE_URL` first, then builds from `POSTGRES_*` vars

**Node SSR (Next.js, Nuxt):**
- `DATABASE_URL` is compatible with Prisma, Drizzle, Knex, Sequelize, and most Node ORMs
- No automatic migrations — the project handles its own schema management

**.NET:**
- `ConnectionStrings__DefaultConnection` follows ASP.NET Core convention
- `appsettings.json` not needed — the deploy pipeline writes all connection info to the `.env` file

**Static Sites (HTML, React, Vue, Svelte, Hugo):**
- No server-side runtime, so no database connection
- Use the **Form API** to accept form submissions from static sites (see below)

### Form API for Static Sites

Static sites can submit form data via a secure API endpoint — no backend needed in the project itself.

**Endpoint:** `POST /api/forms/{project-slug}/`

**Security (all three layers enforced):**

| Layer | Mechanism | Details |
|-------|-----------|----------|
| **API key** | `X-Form-Key` header or `_form_key` body field | Per-project, auto-generated 40-char token. Regenerate from studio UI; old key invalidated immediately. |
| **Rate limiting** | Redis-backed counter | 10 submissions/min per IP per project. Returns `429` when exceeded. |
| **Origin validation** | `Origin`/`Referer` header check | Rejects requests from domains not matching the project's deployed domains. |

Additional: honeypot anti-spam, 100KB size limit, blocked for suspended/archived projects.

**Response codes:** `201` success, `403` invalid key or blocked origin, `404` project not found, `429` rate limited.

**Management:**
- `GET /api/forms/{slug}/list/` — list submissions (project owner/staff only)
- `DELETE /api/forms/{slug}/list/` — bulk delete all submissions
- `GET /api/forms/{slug}/{id}/` — single submission detail
- `DELETE /api/forms/{slug}/{id}/` — delete a single submission
- Per-project API key generated via `Project.get_or_create_form_api_key()`

**Example usage in a static site:**
```html
<form action="https://app.saasclaw.ai/api/forms/my-project/" method="POST">
  <input type="hidden" name="website" value=""> <!-- honeypot -->
  <input type="hidden" name="_form_key" value="YOUR_PROJECT_API_KEY">
  <input type="text" name="name" required>
  <input type="email" name="email" required>
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>
```

Or via JavaScript:
```javascript
fetch('https://app.saasclaw.ai/api/forms/my-project/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-Form-Key': 'YOUR_PROJECT_API_KEY'
  },
  body: JSON.stringify({name: 'Jane', email: 'jane@example.com', message: 'Hello!'})
}).then(r => r.json()).then(data => console.log(data));
```

### Data Storage Summary

| Data Type | Storage Location |
|-----------|----------------|
| Project metadata, users, sessions | SaaSClaw control plane PostgreSQL (`DATABASE_URL` in settings) |
| Project application data | Per-project PostgreSQL on local server (auto-provisioned) |
| Static site form submissions | SaaSClaw control plane PostgreSQL (`FormSubmission` model) |
| Uploaded files / build artifacts | MinIO (S3-compatible object storage) |
| Code | Git bare repos + worktrees |

## Supported Frameworks

Vite (React/Vue/Svelte), Next.js (SSR), Django, Flask, FastAPI, HTMX, Hugo, .NET/C#, static HTML

## Architecture

```
Your UI (Django views, templates, static files)
    ↓ imports
SaaSClaw Engine (saasclaw_engine.*)
    ↓ manages
Git Bare Repos → Worktree Workspace → Deploy Pipeline → Nginx + SSL
```

The engine handles all backend logic. You build the frontend — the wizard, file editor, project dashboard, settings — on top.

## PII Protection

The engine includes built-in PII detection and sanitization to prevent sensitive data from reaching LLM providers. This is critical for HR/payroll, healthcare, financial, and other data-sensitive use cases.

### How It Works

Every message sent to an LLM passes through **PII Guard**, a Presidio-based microservice running on `localhost:8900`. It uses spaCy NLP for context-aware detection plus 14 custom regex recognizers. Consumers call it over HTTP with automatic regex fallback if the service is down.

**Detection patterns:** SSNs, credit card numbers, phone numbers, email addresses, mailing addresses, bank routing/account numbers, dates of birth, passport numbers, driver's licenses, salary/compensation, database connection strings, AWS access keys, and IP addresses.

**Redaction:** Detected values are replaced with synthetic placeholders (`{{SSN}}`, `{{SALARY}}`, `{{EMAIL}}`, etc.) before the message reaches the LLM.

**Fallback:** If the service is unreachable, consumers use identical built-in regex patterns — zero downtime.

See [docs/PII-PROTECTION.md](docs/PII-PROTECTION.md) for the full guide.

### LLM Gateway Mode

For projects that require data never leave your infrastructure, enable **LLM Gateway mode** on the project. This forces all agent requests through a local/self-hosted LLM endpoint (vLLM, Ollama, LM Studio) and blocks cloud providers entirely.

**Configuration:**
```python
# settings.py
LLM_GATEWAY_URL = 'http://your-vllm-server:8080/v1'  # OpenAI-compatible endpoint
LLM_GATEWAY_MODEL = 'meta-llama/Llama-3.1-70B-Instruct'  # or leave empty to use default
LLM_GATEWAY_BLOCKED_PROVIDERS = ['zai', 'openai', 'anthropic', 'google', 'mistral', 'groq']
```

**Per-project toggle:** Staff users can enable `require_gateway` on individual projects. When enabled:
1. Cloud providers in the blocked list are overridden to `local`
2. The LLM base URL is set to `LLM_GATEWAY_URL`
3. PII Guard still runs as defense-in-depth
4. Data never leaves your server

### Defense in Depth

The engine applies multiple layers of protection:

| Layer | What it does | Always active? |
|-------|-------------|---------------|
| **PII Guard** | Presidio + spaCy microservice detects and redacts sensitive patterns | Yes, every LLM call |
| **Regex fallback** | Built-in regex if PII Guard service is down | Automatic |
| **Prompt Injection Guard** | Scans user input for injection patterns (sunglasses library) | Yes, every message |
| **LLM Gateway** | Routes requests to local LLM, blocks cloud providers | Per-project toggle |
| **Audit logging** | Logs redaction counts, injection blocks, and pattern types | Yes |

## Prompt Injection Defense

All user input to the wizard and agent runner is scanned for prompt injection attempts using the [sunglasses](https://github.com/sunglasses-dev/sunglasses) library (1094 patterns, 65 attack categories, 23 languages).

### How It Works

A dual-layer defense scans every message:

1. **Wizard endpoint** — user input is scanned before reaching the agent. Blocked input returns HTTP 422 with severity and findings.
2. **Agent runner** — `run_agent()` scans again as a fallback, catching anything that bypasses the endpoint.

**Detection capabilities:**
- Direct instruction override ("ignore all previous instructions")
- Role-play attacks (DAN, persona switching)
- System prompt extraction attempts
- Unicode evasion (zero-width characters, RTL override, homoglyphs)
- Base64-encoded attacks
- Multimodal scanning (OCR on uploaded images)

**Performance:** <3ms per scan, zero GPU required.

**Audit trail:** Blocked attempts are logged to `/srv/saasclaw/logs/prompt-guard.log` with timestamp, source project, severity, and matched patterns.

**Graceful degradation:** If sunglasses is not installed, all input is allowed (with a log warning).

### What's Not Covered

- **Images/screenshots**: PII Guard is text-only. Image-based PII is not detected.
- **Deployed application data**: PII Guard protects the *build process*. Data in the apps users build is the application's own responsibility.

### Extending PII Guard

Custom PII patterns can be added via the Studio Settings UI (stored in database, loaded by the service on startup). See [docs/PII-PROTECTION.md](docs/PII-PROTECTION.md) for the full guide.

## Testing

365 tests across 16 test files. See [docs/TESTING.md](docs/TESTING.md) for the full guide.

```bash
python3 -m pytest           # run all
python3 -m pytest -v        # verbose
python3 -m pytest -k "form"  # filter by name
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

## Links

- [SaaSClaw](https://saasclaw.ai) — The production platform built on this engine
- [Issues](https://github.com/normandmickey/saasclaw-engine/issues) — Bug reports and feature requests

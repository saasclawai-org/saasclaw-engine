# SaaSClaw Engine

The open-source AI-powered application build, deploy, and agent engine.

[![AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

SaaSClaw Engine is the backend that powers [SaaSClaw](https://saasclaw.ai). It provides the deploy pipeline, AI coding agent system, GitHub integration, and all data models — everything you need to build your own AI app builder on top of.

## What It Does

- **AI Agent** — Run LLM-powered agents (via Pi RPC or direct API) with tools for file I/O, shell commands, git, web search, and todo tracking
- **Deploy Pipeline** — Build and deploy projects to preview and production environments with automatic nginx config, SSL, health checks, and rollback
- **GitHub Integration** — Clone, commit, and push to user repos via a GitHub App (JWT auth + installation tokens)
- **Task Queue** — Async Celery workers for long-running deploy jobs
- **API Key Management** — Encrypted storage for user-provided LLM provider keys

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

### 2. Add to INSTALLED_APPS

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

### 3. Configure settings

```python
# settings.py

# Database — PostgreSQL required
DATABASE_URL = 'postgresql://user:pass@localhost/myapp'

# Redis
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'

# LLM providers (user-provided keys stored encrypted in DB)
# No provider keys needed in settings — users add them via the UI

# Paths
GIT_ROOT = '/srv/myapp/git'           # Bare repos
PROJECT_ROOT = '/srv/myapp/projects'   # Deployed projects
LOG_ROOT = '/srv/myapp/logs'          # Deploy logs

# GitHub App (optional — for connecting user repos)
GITHUB_APP_ID = ''                     # GitHub App ID
GITHUB_APP_PRIVATE_KEY_PATH = ''       # Path to .pem file
GITHUB_WEBHOOK_SECRET = ''             # Webhook secret

# Static files
STATIC_URL = '/static/'
STATIC_ROOT = '/var/www/myapp/static'
```

### 4. Run migrations

```bash
python manage.py migrate
```

### 5. Wire the URLs

```python
# urls.py

from django.urls import path, include
from saasclaw_engine.integrations.views import github_setup, github_webhook

urlpatterns = [
    path('admin/', admin.site.urls),
    path('github/webhook/', github_webhook, name='github_webhook'),
    path('github/setup/', github_setup, name='github_setup'),
    # Your app's URLs
]
```

### 6. Run it

```bash
# Web
python manage.py runserver

# Celery worker (for async deploys)
celery -A myapp worker -l info

# Celery beat (for periodic tasks)
celery -A myapp beat -l info
```

## Engine API

### Deploy Pipeline

```python
from saasclaw_engine.deployments.service import deploy_preview, deploy_production

# Deploy a project's latest commit to preview
deploy_preview(project, user, log_file)

# Promote to production
deploy_production(project, environment, user, log_file)
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

# Clone a user's repo into a worktree
clone_or_update_repo(project, token)

# Commit changes and push
commit_and_push_repo(project, message="Add new feature", token=token)
```

## Project Components

| Package | Description |
|---------|-------------|
| `saasclaw_engine.agent` | Pi Bridge (RPC agent), fallback runner, and agent tools (file I/O, bash, git, web, todos) |
| `saasclaw_engine.deployments` | Models (Project, Environment, Deployment, Domain, EnvVar), deploy pipeline, nginx config generation |
| `saasclaw_engine.integrations` | GitHub App auth, repo clone/push, webhook handling |
| `saasclaw_engine.agents` | Celery task models and async task execution |
| `saasclaw_engine.projects` | Project model with framework, runtime, and config fields |
| `saasclaw_engine.studio_models` | AgentSession, ProviderKey, Workspace, Todo, TokenUsage models |
| `saasclaw_engine.help_search` | RAG-based help search using ChromaDB |

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

## License

AGPL-3.0. See [LICENSE](LICENSE).

## Links

- [SaaSClaw](https://saasclaw.ai) — The production platform built on this engine
- [Issues](https://github.com/normandmickey/saasclaw-engine/issues) — Bug reports and feature requests

# SaaSClaw Engine

The open-source AI-powered app build, deploy, and agent engine behind [SaaSClaw](https://saasclaw.ai).

**Install:**

```bash
pip install saasclaw-engine
```

Then add the engine apps to your Django `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ... your apps ...
    'saasclaw_engine.projects',
    'saasclaw_engine.deployments',
    'saasclaw_engine.integrations',
    'saasclaw_engine.agents',
    'saasclaw_engine.keys',
    'saasclaw_engine.studio_models',
]
```

**What's included:**

| Component | Description |
|-----------|-------------|
| `saasclaw_engine.agent` | Pi Bridge (RPC agent) + fallback runner + agent tools |
| `saasclaw_engine.deployments` | Deploy pipeline: preview/production with nginx, SSL, rollback |
| `saasclaw_engine.integrations` | GitHub App integration: auth, repo clone/push, webhooks |
| `saasclaw_engine.agents` | Celery task service for async deploys |
| `saasclaw_engine.projects` | Project, environment, and domain models |
| `saasclaw_engine.studio_models` | Agent sessions, provider keys, workspaces, todos, token tracking |
| `saasclaw_engine.keys` | Encrypted API key storage |

**Requirements:** Python 3.11+, Django 5.0+, PostgreSQL, Redis, Celery

## License

AGPL-3.0 — see [LICENSE](LICENSE).

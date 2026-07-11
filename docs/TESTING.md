# Testing

## Running Tests

```bash
cd /srv/saasclaw/engine
python3 -m pytest                    # all tests
python3 -m pytest -v                 # verbose
python3 -m pytest saasclaw_engine/projects/tests/  # single module
python3 -m pytest -k "test_form"     # by name
```

Tests use SQLite (no real database needed) and mock external services. Every test method requires `@pytest.mark.django_db` for database access.

## Test Configuration

- **Settings**: `saasclaw_engine/test_settings.py` — SQLite in-memory, minimal INSTALLED_APPS
- **Runner**: pytest with `pytest-django`, configured in `pyproject.toml`
- **Python**: System Python 3.14 with `DJANGO_SETTINGS_MODULE` from pyproject.toml

## Test Suites

### Projects (`projects/tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_models.py` | 8 | `Project.get_or_create_form_api_key()` generation, uniqueness, idempotency; slug validation; default values |
| `test_accounts.py` | 5 | User registration, profile creation, API key generation |
| `test_submissions.py` | 14 | Form submission CRUD, filtering, deletion |
| `test_gateway.py` | 17 | Gateway routing, request forwarding, header handling |

### Studio Models (`studio_models/tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_models.py` | 17 | Workspace creation, branch property, ordering; AgentSession lifecycle, status transitions, profile assignment; AgentMessage ordering, tool_call JSON, cascade deletes |

### Agent (`agent/tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_pii_guard.py` | 102 | PII pattern detection (SSN, email, phone, credit card, API keys), redaction, edge cases, multi-format inputs |
| `test_saasclaw_config.py` | 13 | `.saasclaw` config file loading (file-based, directory-based, `.saasclaw/config.json`), invalid JSON, glob pattern matching |
| `test_prompt_guard.py` | 24 | Prompt injection detection: clean input, injection patterns, Unicode evasion, multimodal scanning, audit logging, graceful degradation |
| `test_pi_bridge.py` | 18 | Provider interface bridge between runner and LLM APIs |

### Deployments (`deployments/tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_utils.py` | 31 | Env file parsing/serialization, command execution helpers |
| `test_service.py` | 19 | Env file loading edge cases, `_serialize_env_file` sorting, Postgres credential naming from slugs (truncation, uniqueness) |
| `test_pipeline.py` | 35 | Secret scanner patterns (AWS, GitHub PAT, private keys, DB strings, OpenAI), directory skipping (.git, node_modules); `_tail_text` truncation; `_slugify_system_name` edge cases; `_normalize_repo_url` SSH/HTTPS conversion; Semgrep malware detection (reverse shells, eval injection, rules file validity) |

### Integrations (`integrations/tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_forms_api.py` | 31 | Form submission API: auth (API key, origin validation), rate limiting, honeypot, payload validation, response codes |
| `test_db_console.py` | 22 | Database console: table listing, schema view, SQL execution, write-mode gating, permission checks |
| `test_github.py` | 5 | GitHub App JWT generation, installation access tokens |

### Agents Tasks (`agents/tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_tasks.py` | 6 | Stale session cleanup logic: 15-minute timeout threshold, status transitions, count returns |

### Integration (`tests/`)

| File | Tests | Covers |
|------|-------|--------|
| `test_staging_integration.py` | 16 | Staging environment setup, deployment workflow end-to-end |

## Totals

**591 tests passing** across 17 test files.

## Writing New Tests

1. Add test file in the appropriate `module/tests/` directory
2. Use `@pytest.mark.django_db` for any test that touches the database
3. Use `tmp_path` fixture for temporary file operations
4. Mock external services (PostgreSQL, systemd, npm, git) — never call them from tests
5. If testing a model from a new app, add it to `test_settings.py` `INSTALLED_APPS`
6. Avoid raw SQL in migrations — use `RunPython` for SQLite compatibility
7. Run the full suite before pushing: `python3 -m pytest -q`

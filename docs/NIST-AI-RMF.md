# NIST AI Risk Management Framework (AI RMF 1.0) Compliance

This document maps SaaSClaw Engine's technical controls to the [NIST AI RMF 1.0](https://www.nist.gov/itl/ai-risk-management-framework) functions: **GOVERN**, **MAP**, **MEASURE**, and **MANAGE**.

---

## GOVERN — Cultivating a Culture of AI Risk Management

### GOVERN 1.1: Policies and Processes

| Control | Implementation |
|---------|---------------|
| AI governance policies | `SiteSettings` singleton model (`studio_models/models.py`) provides staff-tunable governance flags |
| Configurable enforcement | All security features are togglable via admin UI — no hardcoded mandates |
| Audit trail | Deployments are logged with timestamps, git SHA, status, and findings |

### GOVERN 1.3: Roles and Responsibilities

| Control | Implementation |
|---------|---------------|
| Project decommissioning | `decommission_project()` logs all decommission steps to `/srv/saasclaw/logs/decommission.log` including service teardown |
| Staff approval workflow | `project_approval_required` flag gates project creation behind staff review (see `docs/PROJECT-APPROVAL.md`) |
| Required training gate | `require_training_before_project` enforces training completion before project creation |

### GOVERN 1.5: AI Disclosure

| Control | Implementation |
|---------|---------------|
| Mandatory disclosure | `ai_disclosure_required` enforces an AI-generated-content checkbox on project intake |
| Per-session tracking | `AgentSession` records all AI interactions with timestamps and role tags |

### GOVERN 1.6: Human Oversight

| Control | Implementation |
|---------|---------------|
| Wizard-stage gating | AI agent progress flows through `plan → build → review → debug → docs → ship` with explicit human review stages |
| Manual deploy gate | Production deploys are manual — no auto-deploy to production |
| Configurable gateway | `default_require_gateway` keeps LLM data on-server by default |

---

## MAP — Understanding Context and Risk

### MAP 1.1: Identifying AI Risks

| Control | Implementation |
|---------|---------------|
| Secret detection patterns | `_scan_for_secrets()` scans for 10 credential types: AWS keys, GitHub tokens, GitLab tokens, private keys, DB connection strings, API keys, OpenAI keys, passwords |
| Dependency vulnerability scanning | `_scan_dependencies()` runs `npm audit` and `pip check` for known CVEs |
| File size limits | `.saasclaw` config enforces per-file line limits to prevent monolithic, hard-to-audit code |

### MAP 1.2: Categorizing AI Systems

| Control | Implementation |
|---------|---------------|
| Project metadata | Every project records `framework`, `runtime_kind`, environment configuration, and owner |
| Session context | `AgentSession` stores `profile`, `stage`, and `status` for each AI interaction chain |

### MAP 1.5: Impact Assessment

| Control | Implementation |
|---------|---------------|
| PII detection | `detect_pii()` identifies 15+ PII categories: SSNs (US, UK, Canada), credit cards, emails, phone numbers, API keys, bank accounts, medical record numbers |
| Custom PII patterns | `CustomPiiPattern` model allows staff to define organization-specific PII patterns (e.g., internal employee IDs, national ID formats) |
| Sensitive data tracking | `pii_guard_enabled` can be toggled per deployment instance |

### MAP 2.3: Data Governance

| Control | Implementation |
|---------|---------------|
| PII sanitization | `sanitize_for_llm()` and `sanitize_messages()` redact detected PII before sending to LLM providers |
| Regex-based detection | All PII patterns use documented regex — no ML-based inference on user data |
| Multi-format support | Handles SSN dashes, spaces, no separators; credit card with/without spaces; phone in multiple formats |

---

## MEASURE — Analyzing, Assessing, and Tracking AI Risk

### MEASURE 1.2: Testing and Evaluation

| Control | Implementation |
|---------|---------------|
| 365 automated tests | Tests cover secret scanning, PII detection, deploy pipeline, form API security, database console access control |
| Secret scanner tests | 9 tests verify detection of AWS keys, GitHub PATs, private keys, DB strings, and that `.git`/`node_modules` are skipped |
| PII guard tests | 102 tests verify detection and redaction of all PII categories, edge cases, and multi-format inputs |
| Deploy pipeline tests | 19 tests verify env file parsing, credential naming, and Postgres isolation |

### MEASURE 2.1: Performance Monitoring

| Control | Implementation |
|---------|---------------|
| Deploy logging | Every deploy writes to `/srv/saasclaw/projects/{slug}/logs/deploy-{id}.log` |
| Deployment records | `Deployment` model tracks `status`, `started_at`, `finished_at`, `git_commit_sha`, and `error_message` |
| Session monitoring | `AgentSession.updated_at` enables stale session detection (15-minute timeout) |

### MEASURE 2.6: Bias and Security Monitoring

| Control | Implementation |
|---------|---------------|
| Deploy blocking | `block_deploy_on_findings` can be enabled to block deploys with high/critical CVEs or detected secrets |
| Advisory mode (default) | By default, findings are logged as warnings without blocking — adjustable per instance |

---

## MANAGE — Prioritizing and Acting on AI Risk

### MANAGE 1.1: Risk Treatment

| Control | Implementation |
|---------|---------------|
| Incremental deployment | Projects deploy to `preview` first, then require manual `production` deploy |
| Environment isolation | Preview and production get separate: PostgreSQL databases, roles, passwords, systemd services, nginx configs |
| Separate credentials per project | Each project gets unique `db_name`, `db_user`, and `db_password` — no shared credentials |
| Per-project API keys | Form submissions use per-project `form_api_key` — no shared keys |

### MANAGE 1.2: Incident Response

| Control | Implementation |
|---------|---------------|
| Deploy failure capture | Failed deploys log `error_message` (up to 5000 chars) with full stack traces |
| Rollback support | `_refresh_repo_checkout_for_deploy()` enables re-deploying previous commits |
| Decommission logging | `decommission_project()` logs all cleanup steps to persistent log file |

### MANAGE 1.5: Documentation and Communication

| Control | Implementation |
|---------|---------------|
| Architecture docs | `docs/architecture.md` documents all subsystems, data flows, and security decisions |
| Human authorship docs | `HUMAN_AUTHORSHIP_PRODUCT.md` records all significant human decisions and rationale |
| Test documentation | `docs/TESTING.md` documents all 365 tests, how to run them, and testing conventions |

---

## Feature Toggle Reference

All governance controls are configurable via `SiteSettings` (singleton model, admin-accessible):

| Setting | Default | NIST Function | Description |
|---------|---------|---------------|-------------|
| `secret_scan_enabled` | `True` | MAP 1.1 | Scan code for secrets during deploy |
| `dependency_scan_enabled` | `True` | MAP 1.1 | Run npm audit / pip check during deploy |
| `block_deploy_on_findings` | `False` | MANAGE 1.1 | Block deploy on security findings (advisory by default) |
| `ai_disclosure_required` | `True` | GOVERN 1.5 | Require AI content disclosure checkbox |
| `pii_guard_enabled` | `True` | MAP 1.5 | Redact PII before LLM API calls |
| `default_require_gateway` | `False` | GOVERN 1.6 | Default new projects to on-server LLM gateway |
| `project_approval_required` | `False` | GOVERN 1.3 | Require staff approval before project creation |
| `require_training_before_project` | `False` | GOVERN 1.1 | Require training completion before project creation |

---

## Known Limitations

- **PII detection is regex-based** — cannot detect PII in uploaded images/screenshots without OCR (noted as future enhancement requiring GPU)
- **Secret scanner operates on committed code** — does not scan environment variables or runtime secrets
- **Dependency scanning uses upstream audit tools** — accuracy depends on npm/pip vulnerability database freshness
- **AI RMF mapping is self-assessed** — not independently audited

---

## Related Documents

- [PII Protection](docs/PII-PROTECTION.md) — Detailed PII detection and redaction guide
- [Project Approval](docs/PROJECT-APPROVAL.md) — Staff approval workflow documentation
- [Testing](docs/TESTING.md) — Test suite documentation (365 tests)

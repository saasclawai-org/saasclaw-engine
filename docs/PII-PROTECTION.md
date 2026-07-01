# PII Protection in SaaSClaw Engine

This document describes how the SaaSClaw Engine protects sensitive personal information (PII) from being exposed to LLM providers during the AI agent build process.

## Why This Matters

When an AI coding agent builds an application, it reads project files and sends their contents to an LLM as context. If those files contain real employee data — SSNs, salaries, addresses, health information — that data is transmitted to the LLM provider's servers. This creates compliance risk under HIPAA, GLBA, FERPA, GDPR, state privacy laws, and employer liability frameworks.

SaaSClaw Engine addresses this with two complementary mechanisms: **PII Guard** (content-level sanitization via Presidio microservice) and **LLM Gateway Mode** (infrastructure-level isolation).

---

## PII Guard

### Overview

PII Guard is a **Presidio-based microservice** running on `localhost:8900` that detects and redacts sensitive patterns in all LLM-bound messages. It uses spaCy NLP for context-aware detection plus 14 custom regex recognizers for structured patterns.

All consumers (Python engine, Pi coding agent, studio views) call PII Guard over HTTP. If the service is unavailable, they seamlessly fall back to built-in regex patterns.

### Architecture

```
┌─────────────┐     HTTP      ┌──────────────────┐
│ Python Engine│ ──────────────▶│                  │
│ (pii_guard)  │                │  PII Guard API   │
└─────────────┘                │  localhost:8900   │────▶ spaCy
┌─────────────┐     HTTP      │                  │     en_core_web_sm
│ Pi Agent     │ ──────────────▶│  /analyze        │
│ (TS ext)     │                │  /sanitize       │
└─────────────┘                │  /sanitize/messages│
┌─────────────┐     HTTP      │  /health          │
│ Studio Views │ ──────────────▶│  /patterns        │
└─────────────┘                └──────────────────┘
```

### Service Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health, spaCy model status, entity list |
| `/patterns` | GET | List all active recognizers with placeholder mappings |
| `/analyze` | POST | Detect PII in text (returns match positions) |
| `/sanitize` | POST | Detect + redact text with placeholders |
| `/sanitize/messages` | POST | Redact full LLM message list (OpenAI format) |

### Detection Patterns

| Pattern | Example Input | Placeholder | Source |
|---------|--------------|-------------|--------|
| SSN | `123-45-6789` | `{{SSN}}` | Custom regex (validates prefixes, groups) |
| Credit Card | `4111-1111-1111-1111` | `{{CC}}` | Custom regex (Visa, MC, Amex, Discover) |
| Phone Number | `(555) 867-5309` | `({{PHONE}})` | Custom regex |
| Email | `john@company.com` | `{{EMAIL}}` | Custom regex (excludes localhost) |
| Mailing Address | `456 Oak Ave, Springfield, IL 62704` | `{{ADDRESS}}` | Custom regex |
| Bank Routing | `routing: 021000021` | `{{ROUTING}}` | Custom regex (keyword-gated) |
| Bank Account | `account: 1234567890123456` | `{{ACCT}}` | Custom regex (keyword-gated) |
| Salary | `salary: $85,000 per year` | `{{SALARY}}` | Custom regex (keyword-gated) |
| Date of Birth | `DOB: 01/15/1985` | `{{DOB}}` | Custom regex (keyword-gated) |
| Passport | `passport: X12345678` | `{{PASSPORT}}` | Custom regex (keyword-gated) |
| Driver License | `driver's license: D123456789` | `{{DL}}` | Custom regex (keyword-gated) |
| IP Address | `192.168.1.100` | `{{IP}}` | Custom regex |
| AWS Key | `AKIAIOSFODNN7EXAMPLE` | `{{AWS_KEY}}` | Custom regex |
| DB Connection | `postgres://admin:pass@db:5432/mydb` | `{{DB_CONN}}` | Custom regex |

### Context-Aware Detection

Some patterns (bank accounts, DOBs, salaries, DLs, passports, routing numbers) require a **context keyword** to avoid false positives. A bare 9-digit number that happens to match a routing number pattern won't be flagged — it needs to appear near "routing", "account", "salary", etc.

This design choice trades some detection coverage for dramatically fewer false positives. In an HR/payroll context, false positives that redact important code values (port numbers, IDs, quantities) are more harmful than missed patterns.

### How It Integrates

**Runner path** (`agent/runner.py`):
```
User prompt → Agent builds message history → PII Guard (service or regex) sanitizes → _call_llm() → LLM response
```

**PiBridge path** (`agent/pi_bridge.py`):
```
User prompt → PII Guard sanitizes prompt → Pi subprocess (reads files, calls LLM) → Events back
```

**Pi extension path** (`extensions/pii-guard.ts`):
```
Pi internal messages → HTTP call to PII Guard service (regex fallback) → LLM calls sanitized
```

### Fallback Behavior

If the PII Guard service is unreachable, all consumers automatically fall back to the built-in regex patterns. Zero downtime — same API, same behavior, just powered by regex instead of Presidio.

### Logging

The actual sensitive values are **never logged** — only placeholder types and counts:

```bash
# Service logs
journalctl -u pii-guard.service | grep "PII redacted"

# Engine logs
journalctl -u saasclaw-web.service | grep "PII redacted"

# Pi extension logs
cat /var/log/saasclaw/pii-guard.log 2>/dev/null
```

### Custom Patterns

Admins can add custom PII patterns via the Studio Settings UI. These are stored in the database as `CustomPiiPattern` objects and loaded by the PII Guard service on startup.

After adding patterns, restart the service: `sudo systemctl restart pii-guard`

### Service Management

```bash
sudo systemctl status pii-guard      # status
curl -s http://127.0.0.1:8900/health  # health check
sudo systemctl restart pii-guard      # restart
journalctl -u pii-guard -f             # follow logs
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `PII_GUARD_HOST` | `127.0.0.1` | Bind address |
| `PII_GUARD_PORT` | `8900` | Bind port |
| `PII_GUARD_URL` | `http://127.0.0.1:8900` | Client URL (consumers) |
| `PII_GUARD_SPACY_MODEL` | `en_core_web_sm` | spaCy model |
| `PII_GUARD_MIN_SCORE` | `0.5` | Min detection confidence |
| `PII_GUARD_TIMEOUT` | `2.0` | Client timeout (seconds) |

---

## LLM Gateway Mode

For projects that require data never leave your infrastructure, enable **LLM Gateway mode** on the project. This forces all agent requests through a local/self-hosted LLM endpoint and blocks cloud providers entirely.

See the engine README for configuration details.

---

## Coverage Summary

| Area | Status | Notes |
|------|--------|-------|
| Text PII in prompts | ✅ Covered | Presidio NLP + regex on all message content |
| Text PII in file contents | ✅ Covered | Tool results sanitized before next LLM round |
| Pi subprocess internal LLM calls | ✅ Covered | Pi extension calls PII Guard service directly |
| Service downtime | ✅ Covered | Automatic regex fallback, zero downtime |
| Images/screenshots | ❌ Not covered | PII Guard is text-only |
| Deployed app runtime data | ❌ Out of scope | Data in user-built apps is the application's responsibility |

---

## Defense in Depth

| Layer | What it does | Always active? |
|-------|-------------|---------------|
| **PII Guard (service)** | Detects and redacts via Presidio + spaCy | Yes, every LLM call |
| **PII Guard (regex fallback)** | Built-in regex if service is down | Automatic, seamless |
| **Prompt Injection Guard** | Scans for injection patterns | Yes, every message |
| **LLM Gateway** | Routes to local LLM, blocks cloud providers | Per-project toggle |
| **Audit logging** | Logs redaction counts and pattern types | Yes |

---

## Compliance Mapping

| Regulation | PII Guard Helps | Gateway Mode Helps | Notes |
|-----------|-----------------|-------------------|-------|
| HIPAA | Yes (PHI patterns) | Yes (data stays local) | Need BAA if not using gateway |
| GLBA | Yes (financial patterns) | Yes | Covers nonpublic personal information |
| FERPA | Partial | Yes | Add custom patterns for student IDs |
| GDPR | Yes (broad PII coverage) | Yes | Addresses cross-border transfer |
| BIPA | Partial | Yes | Add custom patterns for biometrics |
| State laws | Yes | Yes | Broad coverage for most requirements |

# Project Submission & Approval Workflow

SaaSClaw supports a project approval workflow for instances where staff need to review and approve projects before users can create them — common in enterprise, compliance-heavy, or HR/payroll environments.

## Overview

When enabled, the "New Project" flow changes:

**Without approval (default):** User clicks "New Project" → creates immediately → starts building.

**With approval required:** User clicks "New Project" → fills out a submission form → staff reviews → staff approves or rejects → project is created on approval.

## Enabling

Set in your `.env` or Django settings:

```bash
PROJECT_APPROVAL_REQUIRED=true
```

When off (default), everything works as before — no submission form, no review queue.

## User Flow

### 1. Submit a Project Request

Users visit `/studio/new/` and are redirected to `/studio/submit/` — a form asking for:

| Field | Required | Description |
|-------|----------|-------------|
| Project Name | Yes | What the project will be called |
| Slug | Yes | URL-safe identifier (auto-generated from name) |
| Framework | No | Vite, Next.js, Django, Flask, Hugo, etc. |
| GitHub Repo | No | For importing existing repos |
| Description | Yes | What the project does, its purpose |
| Business Justification | No | Why it's needed, who will use it |
| Data Sensitivity | No | None / Low / Medium / High (PII/PHI) |
| Timeline | No | Urgency, deadline, or priority |

### 2. Track Submissions

Users can view their submissions at `/studio/submissions/` — shows status (Pending/Approved/Rejected), staff notes, and a link to the project once approved.

## Staff Flow

### 1. Review Queue

Staff access the review queue at `/studio/submission-queue/` (also linked from the studio home as "🔒 Review Queue"). The queue shows:

- All submissions with configurable filters (Pending / Approved / Rejected / All)
- Pending count badge
- Full submission details
- Approve/Reject buttons with staff notes
- "Require LLM Gateway" checkbox for PII-sensitive projects

### 2. Approve or Reject

For each pending submission, staff can:

- **Approve** — Creates the project automatically (allocates ports, creates preview/production environments, sets workspace). If "Require LLM Gateway" was checked, `require_gateway=True` is set on the project from the start.
- **Reject** — Closes the submission with optional staff notes explaining why.
- **Add notes** — Visible to the requester on their submissions page.

### 3. Django Admin

`ProjectSubmission` is registered in the Django admin at `/admin/` for full record-keeping and bulk actions.

## Data Sensitivity & Gateway Integration

The submission form includes a **Data Sensitivity** field that helps staff make informed approval decisions:

- **None** — Standard web app, no personal data
- **Low** — User accounts, public profiles
- **Medium** — Internal business data, non-sensitive records
- **High (PII/PHI)** — Employee data, SSNs, health information, financial records

When a submission is marked High, staff should consider:
- Setting "Require LLM Gateway" to force local LLM inference
- Verifying the user understands data handling requirements
- Adding compliance notes (HIPAA, GLBA, FERPA, etc.)

## API

### Submission Endpoints

| URL | Method | Access | Description |
|-----|--------|--------|-------------|
| `/studio/submit/` | GET/POST | Authenticated | Submission form (replaces /studio/new/ when enabled) |
| `/studio/submissions/` | GET | Authenticated | User's own submissions |
| `/studio/submission-queue/` | GET | Staff | All submissions with filters |
| `/studio/submission/<id>/review/` | POST | Staff | Approve or reject a submission |

### Review Request Body

```json
{
  "action": "approve",
  "staff_notes": "Approved for Q3 benefits portal sprint.",
  "require_gateway": true
}
```

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `PROJECT_APPROVAL_REQUIRED` | `false` | Enable the submission/approval workflow |
| `LLM_GATEWAY_URL` | `http://127.0.0.1:8081/v1` | Local LLM endpoint (used when gateway required) |
| `LLM_GATEWAY_MODEL` | `""` | Model name for gateway (falls back to session default) |
| `LLM_GATEWAY_BLOCKED_PROVIDERS` | `['zai', 'openai', 'anthropic', 'google', ...]` | Providers blocked when gateway is active |

## Model: ProjectSubmission

```
ProjectSubmission
├── requester        (FK → User)         — Who submitted
├── name             (CharField)         — Proposed project name
├── slug             (SlugField)         — URL-safe slug
├── description      (TextField)        — What it does
├── framework        (CharField)        — Desired framework
├── source           (CharField)        — blank, template, github
├── template         (CharField)        — Template name
├── repo_url         (URLField)         — GitHub URL
├── business_justification (TextField)  — Why it's needed
├── data_sensitivity (CharField)        — none/low/medium/high
├── estimated_timeline (CharField)      — Urgency
├── status           (choices)          — pending/approved/rejected/cancelled
├── reviewer         (FK → User, nullable) — Staff who reviewed
├── staff_notes      (TextField)        — Internal notes (visible to requester)
├── require_gateway  (BooleanField)     — Pre-set gateway on approval
├── approved_project (OneToOne → Project) — Created on approval
├── created_at / updated_at / reviewed_at
```

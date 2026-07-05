# SaaSClaw Refactor Plan

Created: 2026-07-05
Status: Planning — no changes until each item is approved

## Audit Findings

### Pain Points
- `wizard.py` — 700+ lines, mixes routing/views/business logic
- `helpers.py` — massive, handles context building, workspace mgmt, git, nginx, file operations
- `openclaw_backend.py` and `pi_bridge.py` — two agent backends with overlapping concerns
- `send_message` (non-streaming) and `stream_message` — duplicate conversation building, provider resolution, error handling
- `_build_project_context` in helpers.py — huge function with lots of conditional logic

### Duplication
- Provider/model resolution repeated in `send_message`, `stream_message`, `wizard_view`
- Conversation history building repeated in `send_message` and `stream_message`
- Gateway error handling repeated in `openclaw_backend.py` (retry logic copy-pasted)

### What Works (Don't Touch)
- Deploy pipeline (Celery workers, bare repo model)
- Gateway integration (now correctly pointing to wizard gateway 18790)
- OpenClaw wizard config
- Nginx setup

## Critical Requirements

### Project Isolation
- [ ] Wizard agent must be scoped to its project workspace (cwd + file access)
- [ ] No access to other projects' files, system files, or SaaSClaw app/engine code
- [ ] Options: per-session workspace scoping via gateway config, or bwrap isolation

### Deploy & Server Inspection
- [ ] Wizard agent should be able to inspect its project's deploy status
- [ ] Read project-specific server/systemd logs (e.g. `journalctl -u saasclaw-{slug}-*`)
- [ ] Check deploy output, nginx errors, build failures
- [ ] Visit preview URL to verify changes visually (browser tool)
- [ ] Needs to be scoped — only THAT project's service, not all SaaSClaw services

## Refactor Priorities (ordered)

### Phase 1: Extract shared logic
- [ ] Pull provider/model resolution into a shared helper
- [ ] Pull conversation history building into a shared helper
- [ ] Deduplicate error/retry logic in `openclaw_backend.py`

### Phase 2: Split wizard.py
- [ ] Extract session management (create/end/reset) into a mixin or service
- [ ] Extract git operations (commit, push, ship_it, preview_diff) into a service
- [ ] Keep views thin — routing + render only

### Phase 3: Slim helpers.py
- [ ] Split `_build_project_context` into smaller focused functions
- [ ] Extract workspace operations into their own module
- [ ] Extract deploy-related helpers (nginx, ports) into their own module

### Phase 4: Consolidate agent backends
- [ ] Decide: keep PiBridge or fully commit to OpenClaw gateway?
- [ ] If keeping both, extract a common interface/protocol
- [ ] If dropping PiBridge, remove stream_message's Pi-specific code

### Phase 5: Engine cleanup
- [ ] Audit engine repo for similar issues
- [ ] Clean up runner.py (1800+ lines)

## Notes
- Each phase should be a separate commit, tested before merge
- PiBridge still used by `stream_message` SSE path — can't remove until that's migrated

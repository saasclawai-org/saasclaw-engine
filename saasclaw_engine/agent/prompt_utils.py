"""Prompt building and conversation utilities."""
import json
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)


def _is_permission_question(text: str) -> bool:
    """Check if text ends with a permission-seeking question."""
    import re
    patterns = [
        r'Shall I proceed', r'Should I (?:go ahead|proceed|continue)',
        r'Want me to (?:proceed|continue|go ahead)',
        r'Would you like me to proceed', r'Ready\?', r'OK\?',
        r'Sound(?:s)? good\?', r'Let me know if (?:that|this) works',
        r'Let me know and I', r'Just say the word',
        r'Should I get started', r'Should I start',
        r'Do you want me to (?:go ahead|proceed|start|build)',
        r'Shall I (?:go ahead|start|build|begin)',
    ]
    text_lower = text.lower().rstrip()
    return any(re.search(pat, text_lower) for pat in patterns)


def _strip_permission_question(text: str, has_tool_calls: bool) -> str:
    """If the LLM asks for permission and then makes tool calls, strip the question.

    Models often output a plan, ask 'Shall I proceed?', and then start executing.
    This creates a bad UX because the user sees the question but can't reply
    (the session is locked during tool execution). If tool calls follow,
    just remove the question so it reads as a plan the agent is executing.
    """
    if not has_tool_calls or not text:
        return text
    import re
    patterns = [
        r'(?:^|\n)\s*Shall I proceed\?\s*$',
        r'(?:^|\n)\s*Should I (?:go ahead|proceed|continue)\?\s*$',
        r'(?:^|\n)\s*Want me to (?:proceed|continue|go ahead)\?\s*$',
        r'(?:^|\n)\s*Would you like me to proceed\?\s*$',
        r'(?:^|\n)\s*Ready\?\s*$',
        r'(?:^|\n)\s*OK\?\s*$',
        r'(?:^|\n)\s*Sound good\?\s*$',
        r'(?:^|\n)\s*Sounds good\?\s*$',
        r'(?:^|\n)\s*Let me know if (?:that|this) works (?:for you\?|!)\s*$',
        r'(?:^|\n)\s*Let me know and I\'ll get started\.?\s*$',
        r'(?:^|\n)\s*Just say the word\.?\s*$',
    ]
    result = text
    for pat in patterns:
        result = re.sub(pat, '', result, flags=re.MULTILINE)
    result = result.rstrip() + '\n'
    return result

def _compact_conversation(conversation: list[dict], keep_recent: int = 4) -> list[dict]:
    """Compact older messages to reduce LLM context size and token usage.

    Keeps the most recent ``keep_recent`` tool-result exchanges at full fidelity.
    Older tool results are replaced with one-line summaries.
    Older assistant messages are truncated.
    Older user messages are summarized.
    Old user+assistant pairs that form a complete Q&A are collapsed into a single summary.
    """
    if len(conversation) <= keep_recent * 2:
        return conversation

    tool_indices = [i for i, m in enumerate(conversation) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return conversation

    cutoff_idx = tool_indices[-keep_recent]

    # Group older messages into user-assistant pairs and summarize them
    pre_cutoff = []
    post_cutoff = list(conversation[cutoff_idx:])

    # Build conversation pairs before cutoff
    current_pair = []
    pairs = []
    for i, msg in enumerate(conversation[:cutoff_idx]):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)

        if role in ("user", "assistant"):
            if role == "user" and current_pair:
                pairs.append(current_pair)
                current_pair = []
            current_pair.append((role, content, msg))
        elif role == "tool":
            # Summarize tool results
            tc = msg.get("tool_call") or {}
            tool_name = tc.get("name", "tool") if isinstance(tc, dict) else "tool"
            current_pair.append(("tool", f"[{tool_name}: {len(content)} chars]", msg))
    if current_pair:
        pairs.append(current_pair)

    # Collapse older pairs into summary messages
    summary_count = len(pairs) - keep_recent
    for idx, pair in enumerate(pairs):
        if idx < summary_count:
            # Older pair — collapse to summary
            user_msgs = [c for r, c, _ in pair if r == "user"]
            assistant_msgs = [c for r, c, _ in pair if r == "assistant"]
            if user_msgs:
                user_summary = user_msgs[0][:60]
                if len(user_msgs[0]) > 60:
                    user_summary += "…"
            else:
                user_summary = ""
            if assistant_msgs:
                assistant_summary = assistant_msgs[-1][:80]
                if len(assistant_msgs[-1]) > 80:
                    assistant_summary += "…"
            else:
                assistant_summary = "(action taken)"
            if user_summary and assistant_summary:
                pre_cutoff.append({
                    "role": "assistant",
                    "content": f"[Earlier] User: {user_summary}\nAgent: {assistant_summary}",
                })
            elif assistant_summary:
                pre_cutoff.append({
                    "role": "assistant",
                    "content": f"[Earlier] {assistant_summary}",
                })
        else:
            # Recent pairs — keep but truncate long content
            for role, content, msg in pair:
                new_msg = dict(msg)
                if len(content) > 200:
                    new_msg["content"] = content[:200] + "…"
                pre_cutoff.append(new_msg)

    return pre_cutoff + post_cutoff

def _system_prompt(workspace_path: str, project_name: str, project_notes: str = '', project_directives: str = '', profile_prompt: str = '', allowed_tools: list = None, project_todos: list = None, project_context: str = '') -> str:
    """Build the system prompt for the coding agent."""
    # Detect project type and key conventions
    ctx = _scan_codebase_context(workspace_path)

    # Check project completeness -- tell the agent what already exists
    file_inventory = _scan_project_files(workspace_path)

    notes_section = ''
    if project_notes:
        notes_section += f"\n## Project Notes\n{project_notes}\n"
    if project_todos:
        todo_lines = "\n".join(f"- [{'x' if t['done'] else ' '}] {t['text']}" for t in project_todos)
        notes_section += f"\n## Project Todos\n{todo_lines}\n"
    if project_directives:
        notes_section += f"\n## Agent Directives\n{project_directives}\n"

    # Project context (deploy targets, stack, git workflow) -- injected into ALL stages
    context_section = ''
    if project_context:
        context_section = f"\n{project_context}\n"

    # Profile-specific section
    profile_section = ''
    if profile_prompt:
        profile_section = f"\n## Agent Profile\n{profile_prompt}\n"

    # File inventory so the agent knows what exists WITHOUT reading files
    inventory_section = ''
    if file_inventory:
        inventory_section = f"\n## Existing Files\nYou do NOT need to read these files -- they are listed for your awareness. Only read a file if you need to edit it.\n{file_inventory}\n"

    all_tools = "read_file, write_file, replace_in_file, list_files, git_status, git_diff, git_commit, run_command, web_fetch, web_search, update_todos, apply_patch, background_command, poll_command, spawn_subtask, check_subtask"
    if allowed_tools:
        tools_str = ', '.join(allowed_tools)
    else:
        tools_str = all_tools

    # Load .saasclaw project config for platform hints and architecture rules
    _saasclaw_config = {}
    try:
        from saasclaw_engine.agent.tools import _load_saasclaw_config as _load_cfg
        _saasclaw_config = _load_cfg(workspace_path)
    except Exception:
        pass

    saasclaw_section = ""
    if _saasclaw_config:
        arch_rules = _saasclaw_config.get("architecture", {}).get("rules", [])
        if arch_rules:
            saasclaw_section += "\n## Project Configuration (.saasclaw)\n"
            saasclaw_section += "\n".join(f"- {r}" for r in arch_rules)
            saasclaw_section += "\n"
        platform = _saasclaw_config.get("platform", {})
        if platform:
            saasclaw_section += "\n## Platform Services\n"
            if platform.get("database"):
                saasclaw_section += f"- Database: {platform['database']}\n"
            if platform.get("forms_api"):
                forms = platform["forms_api"]
                for endpoint, desc in forms.items():
                    saasclaw_section += f"- {endpoint}: {desc}\n"
            if platform.get("auth"):
                saasclaw_section += f"- Auth: {platform['auth']}\n"
            saasclaw_section += "\n"

    return f"""You are SaaSClaw Studio, an expert coding agent.

CRITICAL: Never ask the user for permission, confirmation, or approval. Never say "Shall I proceed?", "Want me to continue?", "Should I go ahead?", or anything similar. The user told you what they want -- just do it. Execute tool calls immediately. Every time you ask a question instead of acting, you lock the user out for minutes.

BE EFFICIENT:
- Solve the task in as few tool calls as possible. Small changes = 1-3 tool calls, not 10.
- Don't over-engineer. If a 5-line change works, don't refactor the whole file.
- Don't probe or test extensively before making the change. Read the file, make the change, move on.
- Batch related operations: read multiple files at once, make multiple edits in one replace_in_file call.
- Don't write lengthy explanations before code changes. Brief status, then code.
- If you find yourself doing more than 15 tool calls, reassess. There's probably a simpler approach.

Project: {project_name}
{ctx}{saasclaw_section}{context_section}{inventory_section}{notes_section}{profile_section}
Tools: {tools_str}

Rules:
- The file inventory above shows what exists. Do NOT re-read files you don't need to edit.
- For files over 200 lines, use start_line and end_line to read in sections of ~200 lines. Never try to read a massive file all at once.
- For small changes, use replace_in_file (search/replace blocks) instead of write_file.
- Use write_file only for new files or complete rewrites.
- Use web_search to find current docs or solutions.
- Use web_fetch to read a specific URL.
- Run tests after changes when possible.
- Commit with clear messages.
- DO NOT manually deploy (npm run build, next build, etc.) -- the system auto-deploys on commit. Just commit your changes and tell the user deployment is in progress.
- COMPANY DIRECTORY (CRITICAL): If the project context mentions a Company Directory API, you MUST proxy it from your backend. Do NOT create mock data, fake clients, or hardcoded company records. Your backend calls the platform API (using the X-Company-Key header), your frontend calls your backend.
- ACTION > EXPLANATION: When the user asks for something, your FIRST tool call MUST be replace_in_file or write_file. Never output a plan/analysis without writing code. Read the file, then IMMEDIATELY write the fix.
- MODULAR CODE (CRITICAL): For Next.js/React projects, NEVER inline game/app logic into page.tsx. Always create a custom hook in src/hooks/ (e.g., useCheckers.ts) and a pure-logic module in src/lib/ (e.g., checkers.ts). page.tsx should only import hooks and dispatch between phases. If you are adding a new game: FIRST create src/lib/<game>.ts, THEN create src/hooks/use<Game>.ts, THEN add a thin import + dispatch case to page.tsx. Inlining logic into page.tsx is the #1 failure mode and causes timeouts and broken builds. If a .saasclaw config exists in the project root, FOLLOW its file_limits and architecture.rules exactly — writes that exceed configured limits will be BLOCKED.
- Be concise in explanations. Maximum 2 sentences before or after a code change.
- Use update_todos to plan tasks before starting work and mark items done as you complete them.
- NEVER ask the user for permission or confirmation. Just plan and execute. Asking "Shall I proceed?" or "Want me to continue?" blocks the user for the entire duration of your tool calls. Trust the user's initial request and build it.
- STEP-BY-STEP EXECUTION: For tasks involving 3+ files or multiple phases (scaffolding, logic, styling, data, tests), break the work into numbered steps and execute them sequentially. Write each step's files, then commit before moving to the next step. This gives users visible progress and prevents context loss. Example: Step 1 - create project structure and config → Step 2 - implement core logic → Step 3 - add UI/views → Step 4 - tests → Step 5 - final polish. Commit between steps.
- FILE SIZE DISCIPLINE: Never create files over 500 lines. If editing a file that's already large, extract logic into separate modules FIRST before adding more. Large monolithic files cause timeouts, make diffs unreadable, and prevent reuse. When in doubt, split."""

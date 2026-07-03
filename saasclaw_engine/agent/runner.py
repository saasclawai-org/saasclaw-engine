"""Agent runner -- the core agentic loop.

Receives a user message, calls an LLM, executes any requested tools,
feeds results back, and returns the final response.

Supports multiple LLM backends via STUDIO_LLM_PROVIDER:
  - "zai"       → Z.ai GLM (default, OpenAI-compatible)
  - "openai"    → OpenAI GPT
  - "anthropic" → Anthropic Claude
  - "local"     → Local llama.cpp server

All OpenAI-compatible backends share one code path.
Anthropic uses its own message format.
"""
import concurrent.futures
import json
import logging
import os
import subprocess
import time
import threading
from urllib import request as urllib_request
from urllib.error import URLError, HTTPError

from django.conf import settings

# Thread-safe activity tracker: {session_id: {"text": str, "tools_run": int}}
_agent_activity: dict[str, dict] = {}
_activity_lock = threading.Lock()

def get_agent_activity(session_id: str) -> dict:
    with _activity_lock:
        entry = _agent_activity.get(session_id)
        if entry:
            return {"text": entry["text"], "tools_run": entry["tools_run"]}
        return {}

def set_agent_activity(session_id: str, text: str, tools_run: int = 0):
    with _activity_lock:
        _agent_activity[session_id] = {"text": text, "tools_run": tools_run}

def clear_agent_activity(session_id: str):
    with _activity_lock:
        _agent_activity.pop(session_id, None)

from . import tools as agent_tools

from .pii_guard import sanitize_messages
logger = logging.getLogger(__name__)

# Approximate pricing per 1M tokens (USD)
LLM_PRICING = {
    "zai": {"glm-5": (0.60, 2.00), "glm-4": (0.60, 2.00)},
    "openai": {"gpt-5.5": (5.00, 15.00), "gpt-5.4": (3.00, 12.00)},
    "anthropic": {"claude-4-opus": (15.00, 75.00), "claude-4-sonnet": (3.00, 15.00), "claude-4-haiku": (0.80, 4.00), "claude-3": (3.00, 15.00)},
    "groq": {"gpt-oss-120b": (0.15, 0.30), "gpt-oss-20b": (0.075, 0.15), "llama-3.3-70b": (0.59, 0.79)},
}

def _estimate_cost(provider, model, prompt_tokens, completion_tokens):
    pk = provider.lower()
    ml = model.lower()
    if pk not in LLM_PRICING:
        return 0.0
    for prefix, (ip, op) in LLM_PRICING[pk].items():
        if prefix in ml:
            return round((prompt_tokens / 1_000_000 * ip) + (completion_tokens / 1_000_000 * op), 6)
    return 0.0

_last_usage = {}


MAX_TOOL_ROUNDS = 30  # Cap LLM round-trips per turn (was 100)
MAX_TOTAL_TOOL_CALLS = 60  # Hard cap on total tool calls per turn (was 300)
MAX_TOOL_COST_PER_TURN = 0.50  # $0.50 USD per turn before forced stop (Zai pricing)
EFFICIENCY_WARNING_THRESHOLD = 12  # Warn the model to wrap up after this many calls


# ---------------------------------------------------------------------------
# Provider configurations
# ---------------------------------------------------------------------------

def _provider_config(session_override: str = None, model_override: str = None, user=None) -> dict:
    """Get the active LLM provider + credentials from settings/env.

    Args:
        session_override: If provided (from session), use this provider.
        model_override: If provided, use this specific model name.
        user: If provided, check for user's own API keys first (BYO-key).
    """
    provider = (session_override or os.environ.get("STUDIO_LLM_PROVIDER", "zai")).strip().lower()

    # Check for user's own API key (BYO-key)
    user_key = None
    if user and provider != "local":
        try:
            from saasclaw_engine.studio_models.models import ProviderKey
            pk = ProviderKey.objects.filter(user=user, provider=provider, is_active=True).first()
            if pk and pk.api_key:
                user_key = pk.api_key
        except Exception:
            pass

    configs = {
        "zai": {
            "provider": "zai",
            "api_key": user_key or os.environ.get("ZAI_API_KEY", getattr(settings, "ZAI_API_KEY", "")),
            # Coding plan endpoint for text models; fall back to standard API for vision models
            "base_url": os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4"),
            "model": model_override or os.environ.get("STUDIO_MODEL", "glm-5.1"),
            "format": "openai",
        },
        "openai": {
            "provider": "openai",
            "api_key": user_key or os.environ.get("OPENAI_API_KEY", getattr(settings, "OPENAI_API_KEY", "")),
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "model": model_override or os.environ.get("STUDIO_MODEL", "gpt-5.5"),
            "format": "openai",
        },
        "anthropic": {
            "provider": "anthropic",
            "api_key": user_key or os.environ.get("ANTHROPIC_API_KEY", getattr(settings, "ANTHROPIC_API_KEY", "")),
            "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            "model": model_override or os.environ.get("STUDIO_MODEL", "claude-sonnet-4-6"),
            "format": "anthropic",
        },
        "local": {
            "provider": "local",
            "api_key": "no-key",
            "base_url": os.environ.get("STUDIO_LOCAL_URL", "http://127.0.0.1:8081/v1"),
            "model": model_override or os.environ.get("STUDIO_MODEL", ""),
            "format": "openai",
        },
        "groq": {
            "provider": "groq",
            "api_key": user_key or os.environ.get("GROQ_API_KEY", getattr(settings, "GROQ_API_KEY", "")),
            "base_url": "https://api.groq.com/openai/v1",
            "model": model_override or "openai/gpt-oss-120b",
            "format": "openai",
        },
    }

    return configs.get(provider, configs["zai"])


# Catalog of available models per provider, for the UI dropdown
AVAILABLE_MODELS = {
    "groq": {
        "label": "Groq",
        "models": [
            {"id": "openai/gpt-oss-120b", "name": "GPT-OSS 120B", "context": 131072, "vision": False},
            {"id": "openai/gpt-oss-20b", "name": "GPT-OSS 20B", "context": 131072, "vision": False},
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B", "context": 131072, "vision": False},
            {"id": "groq/compound", "name": "Compound (auto+web)", "context": 131072, "vision": False},
        ],
    },
    "zai": [
        {"model": "glm-5-turbo", "label": "GLM-5 Turbo (fast, cheap)", "vision": False},
        {"model": "glm-5.2", "label": "GLM-5.2 (latest, 1M context)", "vision": False},
        {"model": "glm-5.1", "label": "GLM-5.1 (long-horizon agent)", "vision": False},
        {"model": "glm-5", "label": "GLM-5 (foundation)", "vision": False},
        {"model": "glm-5v-turbo", "label": "GLM-5V-Turbo (vision)", "vision": True},
        {"model": "glm-4.6", "label": "GLM-4.6 (legacy, fast)", "vision": False},
    ],
    "openai": [
        {"model": "gpt-5.5", "label": "GPT-5.5 (flagship, reasoning)", "vision": True},
        {"model": "gpt-5.4-mini", "label": "GPT-5.4 mini (fast, cheap)", "vision": True},
        {"model": "gpt-5.4-nano", "label": "GPT-5.4 nano (fastest, cheapest)", "vision": True},
    ],
    "anthropic": [
        {"model": "claude-fable-5", "label": "Claude Fable 5 (most capable)", "vision": True},
        {"model": "claude-opus-4-8", "label": "Claude Opus 4.8", "vision": True},
        {"model": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "vision": True},
        {"model": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (fast, cheap)", "vision": True},
    ],
    "local": [
        {"model": "", "label": "Server default"},
    ],
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

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

    return f"""You are SaaSClaw Studio, an expert coding agent.


    # Load .saasclaw project config for platform hints and architecture rules
    _saasclaw_config = {}
    try:
        from saasclaw_engine.agent.tools import _load_saasclaw_config as _load_cfg
        _saasclaw_config = _load_cfg(workspace_path)
    except Exception:
        pass

    saasclaw_section = ""
    if _saasclaw_config:
        # Architecture rules from .saasclaw config
        arch_rules = _saasclaw_config.get("architecture", {}).get("rules", [])
        if arch_rules:
            saasclaw_section += "\n## Project Configuration (.saasclaw)\n"
            saasclaw_section += "\n".join(f"- {r}" for r in arch_rules)
            saasclaw_section += "\n"
        # Platform info (database, API proxy) from .saasclaw config
        platform = _saasclaw_config.get("platform", {})
        if platform:
            saasclaw_section += "\n## Platform Services\n"
            if platform.get("database"):
                saasclaw_section += f"- Database: {platform['database']}\n"
            if platform.get("api_proxy"):
                saasclaw_section += f"- API proxy: {platform['api_proxy']} → {platform.get('api_target', 'Django')}\n"
            saasclaw_section += "\n"

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
- ACTION > EXPLANATION: When the user asks for something, your FIRST tool call MUST be replace_in_file or write_file. Never output a plan/analysis without writing code. Read the file, then IMMEDIATELY write the fix.
- MODULAR CODE (CRITICAL): For Next.js/React projects, NEVER inline game/app logic into page.tsx. Always create a custom hook in src/hooks/ (e.g., useCheckers.ts) and a pure-logic module in src/lib/ (e.g., checkers.ts). page.tsx should only import hooks and dispatch between phases. If you are adding a new game: FIRST create src/lib/<game>.ts, THEN create src/hooks/use<Game>.ts, THEN add a thin import + dispatch case to page.tsx. Inlining logic into page.tsx is the #1 failure mode and causes timeouts and broken builds. If a .saasclaw config exists in the project root, FOLLOW its file_limits and architecture.rules exactly — writes that exceed configured limits will be BLOCKED.
- Be concise in explanations. Maximum 2 sentences before or after a code change.
- Use update_todos to plan tasks before starting work and mark items done as you complete them.
- NEVER ask the user for permission or confirmation. Just plan and execute. Asking "Shall I proceed?" or "Want me to continue?" blocks the user for the entire duration of your tool calls. Trust the user's initial request and build it.
- STEP-BY-STEP EXECUTION: For tasks involving 3+ files or multiple phases (scaffolding, logic, styling, data, tests), break the work into numbered steps and execute them sequentially. Write each step's files, then commit before moving to the next step. This gives users visible progress and prevents context loss. Example: Step 1 - create project structure and config → Step 2 - implement core logic → Step 3 - add UI/views → Step 4 - tests → Step 5 - final polish. Commit between steps.
- FILE SIZE DISCIPLINE: Never create files over 500 lines. If editing a file that's already large, extract logic into separate modules FIRST before adding more. Large monolithic files cause timeouts, make diffs unreadable, and prevent reuse. When in doubt, split."""


def _scan_project_files(workspace_path: str) -> str:
    """Quick scan of project files -- gives the agent a map of editable files only."""
    skip = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'dist', 'build', 
            'out', '.next', '.cache', 'coverage', '.mypy_cache', '.pytest_cache', '.ruff_cache',
            '.nuxt', '.output', 'staticfiles', 'media', '__pypackages__', '.eggs', '*.egg-info'}
    skip_ext = {'.pyc', '.pyo', '.min.js', '.min.css', '.min.json', '.map', '.lock',
                '.log', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp', '.woff', '.woff2',
                '.ttf', '.eot', '.wasm', '.bin', '.exe', '.dll', '.so', '.dylib', '.zip', '.tar',
                '.gz', '.sqlite', '.sqlite3', '.db'}
    skip_files = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'Pipfile.lock',
                  'poetry.lock', '.env', '.env.local', '.env.production', 'bun.lockb'}
    # Only show files the agent can meaningfully edit
    editable_ext = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.scss', '.less',
                    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.md', '.txt',
                    '.sh', '.bash', '.sql', '.graphql', '.gql', '.xml', '.csv', '.rb', '.php',
                    '.go', '.rs', '.java', '.kt', '.swift', '.vue', '.svelte', '.astro', '.mdx'}
    lines = []
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in sorted(dirs) if d not in skip]
        rel = os.path.relpath(root, workspace_path)
        if rel == '.':
            rel = ''
        for f in sorted(files):
            if f in skip_files:
                continue
            _, ext = os.path.splitext(f)
            if ext in skip_ext:
                continue
            if ext and ext not in editable_ext:
                continue
            path = f"{rel}/{f}" if rel else f
            size = os.path.getsize(os.path.join(root, f))
            if size > 500:
                lines.append(f"  {path} ({size}b)")
            else:
                lines.append(f"  {path}")
        if len(lines) > 50:
            lines.append("  ... (more files)")
            break
    return "\n".join(lines) if lines else ""


def _patch_context_on_tool(project_id, tool_name, tool_args, tool_result):
    """Incrementally update cached project context when agent writes files."""
    if tool_result.startswith("Error:"):
        return
    try:
        from saasclaw_engine.projects.models import Project
        project = Project.objects.get(id=project_id)
        if not project.context_cache:
            return
        _do_patch_context(project, tool_name, tool_args, tool_result)
    except Exception:
        import logging
        logging.getLogger(__name__).debug("context patch failed", exc_info=True)


def _do_patch_context(project, tool_name, tool_args, tool_result):
    """Actual patching logic."""
    if tool_name == "write_file":
        path = tool_args.get("path", "")
        content_text = tool_args.get("content", "")
        if not path or not content_text:
            return
        lines = project.context_cache
        patched = lines
        Q = chr(34)
        
        # New file not in listing
        if path not in lines:
            patched += "\n  " + path
        
        # Extract types
        new_types = []
        for line in content_text.split("\n"):
            s = line.strip()
            if s.startswith("type ") and "=" in s:
                new_types.append(s.split("#")[0].strip().rstrip("{"))
            elif s.startswith("interface ") and "{" in s:
                new_types.append(s.split("{")[0].strip().rstrip(":"))
            elif s.startswith("export type ") and "=" in s:
                new_types.append(s.split("#")[0].strip())
            elif s.startswith("export interface "):
                new_types.append(s.split("{")[0].strip())
        
        for t in new_types:
            if t not in patched:
                if "Existing type definitions" not in patched:
                    patched += "\n\nExisting type definitions (MUST preserve these):"
                patched += "\n  " + path + ": " + t
        
        # Extract local imports
        new_imports = []
        for line in content_text.split("\n"):
            s = line.strip()
            if s.startswith("from " + Q + "@/"):
                mod = s.split(Q)[1] if Q in s else ""
                if mod.startswith("@/"):
                    new_imports.append(mod[2:])
            elif s.startswith("from " + Q + "./") or s.startswith("from " + Q + "../"):
                mod = s.split(Q)[1] if Q in s else ""
                new_imports.append(mod)
        
        filtered = [m for m in new_imports if m and "react" not in m]
        for m in filtered:
            if m not in patched:
                if "Local modules imported:" not in patched:
                    patched += "\n\nLocal modules imported: "
                if "Local modules imported:" in patched:
                    patched = patched.rstrip() + ", " + m
        
        # Python requirements
        if path == "requirements.txt":
            for line in content_text.split("\n"):
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("-"):
                    pkg = s.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
                    if pkg and pkg not in patched:
                        if "Installed packages:" in patched:
                            patched = patched.rstrip() + ", " + pkg
                        elif "NEVER rewrite requirements.txt" in patched:
                            patched += "\nInstalled packages: " + pkg
        
        if patched != lines:
            project.context_cache = patched
            from saasclaw_engine.projects.models import Project
            Project.objects.filter(id=project.id).update(context_cache=patched)
    
    elif tool_name == "replace_in_file":
        path = tool_args.get("path", "")
        edits = tool_args.get("edits", [])
        if not path or not edits:
            return
        lines = project.context_cache
        patched = lines
        Q = chr(34)
        
        new_types = []
        new_imports = []
        for edit in edits:
            replace_text = edit.get("replace", "")
            for line in replace_text.split("\n"):
                s = line.strip()
                if s.startswith("type ") and "=" in s:
                    new_types.append(s.split("#")[0].strip().rstrip("{"))
                elif s.startswith("interface ") and "{" in s:
                    new_types.append(s.split("{")[0].strip().rstrip(":"))
                elif s.startswith("from " + Q + "@/"):
                    mod = s.split(Q)[1] if Q in s else ""
                    if mod.startswith("@/"):
                        new_imports.append(mod[2:])
        
        for t in new_types:
            if t not in patched:
                if "Existing type definitions" not in patched:
                    patched += "\n\nExisting type definitions (MUST preserve these):"
                patched += "\n  " + path + ": " + t
        for m in new_imports:
            if m and m not in patched and "react" not in m:
                if "Local modules imported:" not in patched:
                    patched += "\n\nLocal modules imported: "
                if "Local modules imported:" in patched:
                    patched = patched.rstrip() + ", " + m
        
        if patched != lines:
            project.context_cache = patched
            from saasclaw_engine.projects.models import Project
            Project.objects.filter(id=project.id).update(context_cache=patched)



def _scan_codebase_context(workspace_path: str) -> str:
    """Auto-detect project type, test command, and conventions from files."""
    hints = []

    # Detect framework/language from key files
    files_present = set()
    try:
        for name in os.listdir(workspace_path):
            files_present.add(name)
    except Exception:
        pass

    if "manage.py" in files_present:
        hints.append("Type: Django project")
        hints.append("Test: python manage.py test")
        hints.append("Dev server: python manage.py runserver")
    elif "package.json" in files_present:
        hints.append("Type: Node.js project")
        # Check for specific frameworks and Node version
        try:
            with open(os.path.join(workspace_path, "package.json")) as f:
                pkg = json.load(f)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "next" in deps:
                    hints.append("Framework: Next.js")
                    hints.append("Test: npm test")
                elif "react" in deps:
                    hints.append("Framework: React")
                    hints.append("Test: npm test")
                elif "express" in deps:
                    hints.append("Framework: Express")
                if "tailwindcss" in deps:
                    hints.append("CSS: Tailwind")
                # Detect required Node version
                node_version = None
                if os.path.exists(os.path.join(workspace_path, ".node-version")):
                    with open(os.path.join(workspace_path, ".node-version")) as nv:
                        node_version = nv.read().strip()
                if not node_version:
                    engines = pkg.get("engines", {})
                    node_version = engines.get("node", "").lstrip("^~>=").split(".")[0] if engines.get("node") else None
                if node_version:
                    hints.append(f"Node version: {node_version} -- run 'fnm use {node_version}' before npm/test commands")
        except Exception:
            pass
    elif "Cargo.toml" in files_present:
        hints.append("Type: Rust project")
        hints.append("Test: cargo test")
        hints.append("Build: cargo build")
    elif "go.mod" in files_present:
        hints.append("Type: Go project")
        hints.append("Test: go test ./...")
    elif "requirements.txt" in files_present or "pyproject.toml" in files_present:
        hints.append("Type: Python project")
        if "pytest" in str(files_present) or os.path.exists(os.path.join(workspace_path, "pytest.ini")):
            hints.append("Test: pytest")
        else:
            hints.append("Test: python -m pytest")

    # Check for common config files
    if ".pre-commit-config.yaml" in files_present:
        hints.append("Linting: pre-commit hooks configured")
    if "tox.ini" in files_present or ".flake8" in files_present:
        hints.append("Linting: flake8/tox")
    if "eslint" in str(files_present):
        hints.append("Linting: ESLint")

    if not hints:
        return "Top-level: " + "  ".join(sorted(files_present)[:20])

    # Add architecture guidance for known project types
    if any("Django" in h for h in hints):
        hints.append("")
        hints.append("Architecture: keep views.py thin (request/response only).")
        hints.append("Business logic → services.py or services/ package (one service per domain).")
        hints.append("Complex queries → model methods or managers, not inline ORM in views.")
        hints.append("Forms/validation → forms.py.")
        hints.append("Permissions/authorization → policies.py or policies/ package (one policy per domain).")
        hints.append("Constants → a constants.py or settings.")
        hints.append("Never put imports, helpers, or business logic at the top of views.py.")
        hints.append("")
        hints.append("File size limits (CRITICAL):")
        hints.append("- NEVER create files over 500 lines. If a file is growing past 500 lines, SPLIT IT.")
        hints.append("- models.py: one model per concern. If models.py exceeds 300 lines, split into models/ package.")
        hints.append("- views.py: one view per URL. If views.py exceeds 200 lines, split into views/ package.")
        hints.append("- services.py: if it exceeds 200 lines, convert to services/ package with one file per domain.")
        hints.append("")
        hints.append("Django conventions:")
        hints.append("- Each app should have: models.py, views.py (or views/), services.py (or services/), urls.py, admin.py, apps.py")
        hints.append("- Views: parse request → call service → return response. No ORM queries in views.")
        hints.append("- Services: contain all business logic and side effects. Raise exceptions for error cases.")
        hints.append("- Policies: contain permission checks (can_user_edit_project, can_deploy, etc.). Import in views.")
        hints.append("- Models: data representation and relationships only. Custom managers for table-level queries.")
        hints.append("- Tests: mirror app structure (tests/test_models.py, tests/test_views.py, tests/test_services.py).")
        hints.append("- Never import models across apps directly — use service layer methods.")

        hints.append("Testing:")
        hints.append("- Test command: python manage.py test --settings=config.test_settings")
        hints.append("- Tests use SQLite in-memory (no PostgreSQL needed).")
        hints.append("- Do NOT run manage.py commands from /srv/saasclaw/projects/ -- only use the current workspace directory.")
        hints.append("- If config/test_settings.py is missing, create it with SQLite and SECRET_KEY='***'.")
        hints.append("")
        hints.append("Preserving scaffold:")
        hints.append("- NEVER rewrite requirements.txt from scratch -- use replace_in_file to ADD new dependencies.")
        hints.append("- NEVER rewrite INSTALLED_APPS from scratch -- use replace_in_file to ADD new entries.")
        hints.append("- NEVER overwrite config/settings.py -- use replace_in_file for changes.")
        hints.append("- The scaffold includes: django-jazzmin, whitenoise, gunicorn, psycopg[binary], pytest, pytest-django.")
        hints.append("")
        hints.append("Testing:")
        hints.append("- Test command: python manage.py test --settings=config.test_settings")
        hints.append("- Tests use SQLite in-memory (no PostgreSQL needed).")
        hints.append("- Do NOT run manage.py commands from /srv/saasclaw/projects/ -- only use the current workspace directory.")
        hints.append("- If config/test_settings.py is missing, create it with SQLite and SECRET_KEY='test'.")
    elif any("Node.js" in h for h in hints) or any("Next.js" in h for h in hints) or any("React" in h for h in hints):
        hints.append("")
        hints.append("Architecture: keep route handlers thin.")
        hints.append("Business logic → lib/ or services/ directory.")
        hints.append("Data access → separate db/ or models/ module.")
        hints.append("API handlers → validate input, call service, format response.")
        hints.append("Shared utilities → utils/ or lib/.")
        hints.append("")
        hints.append("File size limits (CRITICAL):")
        hints.append("- NEVER create files over 500 lines. If a file is growing past 500 lines, SPLIT IT.")
        hints.append("- React components: one component per file. If a component exceeds 200 lines, extract sub-components into separate files.")
        hints.append("- page.tsx/page.jsx: keep under 150 lines. Extract game/app logic into lib/, components/, or hooks/ directories.")
        hints.append("- A monolithic page.tsx with embedded game logic, state, and UI is NEVER acceptable.")
        hints.append("")
        hints.append("Next.js/React conventions:")
        hints.append("- src/app/page.tsx → thin shell that imports and composes components. No business logic here.")
        hints.append("- src/components/ → one component per file, named match (e.g., GameBoard.tsx, not index.tsx)")
        hints.append("- src/lib/ or src/services/ → game rules, API calls, business logic, data transformations")
        hints.append("- src/hooks/ → custom React hooks (useGameState, usePlayer, etc.)")
        hints.append("- src/types/ → shared TypeScript interfaces and types")
        hints.append("")
        hints.append("Platform data persistence (IMPORTANT):")
        hints.append("- Static/SPA projects (node_static deploy) have access to a Django-backed API at /api/forms/.")
        hints.append("- Each project automatically gets a PostgreSQL database. The DATABASE_URL is in the project's .env file.")
        hints.append("- To add custom API endpoints for data persistence: create Django models in the SaaSClaw app and expose views at /api/<slug>/.")
        hints.append("- The nginx config already proxies /api/ requests to Django — no need to change the deploy target from node_static to a Node server.")
        hints.append("- React apps can fetch('/api/<slug>/data') to read/write data through Django + PostgreSQL.")
        hints.append("- Do NOT propose Express/server-side backends — use the existing Django + PostgreSQL infrastructure.")
        hints.append("- src/app/api/<name>/route.ts → API routes, one concern per route, keep under 100 lines")
        hints.append("- State management: useReducer for complex state (games, multi-step flows). Lift state to parent, pass via props.")
        hints.append("- NEVER inline all game/app logic in a single useState or useEffect in page.tsx.")
        hints.append("- Extract reusable logic into custom hooks so components stay declarative.")
        hints.append("- Use barrel exports (index.ts) only for directories with 3+ modules.")
        hints.append("")
        hints.append("File splitting rules:")
        hints.append("- If adding a new game/feature: create src/components/GameName.tsx + src/lib/gameName.ts (rules/logic) + add to page.tsx as import.")
        hints.append("- If editing page.tsx and it's already 150+ lines: REFACTOR by extracting logic to lib/ and components/ BEFORE adding more.")
        hints.append("- If a single component needs multiple sub-views: create a src/components/GameName/ directory with separate files.")
    elif any("Rust" in h for h in hints):
        hints.append("")
        hints.append("Architecture: keep main.rs / handlers thin.")
        hints.append("Business logic → separate modules.")
        hints.append("Data access → model or repository modules.")
    elif any("Go" in h for h in hints):
        hints.append("")
        hints.append("Architecture: keep handlers thin.")
        hints.append("Business logic → internal/ or service/ package.")
        hints.append("Data access → store/ or repository package.")

    return "\n".join(hints)


def _patch_context_on_tool(project_id, tool_name, tool_args, tool_result):
    """Incrementally update cached project context when agent writes files."""
    if tool_result.startswith("Error:"):
        return
    try:
        from saasclaw_engine.projects.models import Project
        project = Project.objects.get(id=project_id)
        if not project.context_cache:
            return
        _do_patch_context(project, tool_name, tool_args, tool_result)
    except Exception:
        import logging
        logging.getLogger(__name__).debug("context patch failed", exc_info=True)


def _do_patch_context(project, tool_name, tool_args, tool_result):
    """Actual patching logic."""
    if tool_name == "write_file":
        path = tool_args.get("path", "")
        content_text = tool_args.get("content", "")
        if not path or not content_text:
            return
        lines = project.context_cache
        patched = lines
        Q = chr(34)
        
        # New file not in listing
        if path not in lines:
            patched += "\n  " + path
        
        # Extract types
        new_types = []
        for line in content_text.split("\n"):
            s = line.strip()
            if s.startswith("type ") and "=" in s:
                new_types.append(s.split("#")[0].strip().rstrip("{"))
            elif s.startswith("interface ") and "{" in s:
                new_types.append(s.split("{")[0].strip().rstrip(":"))
            elif s.startswith("export type ") and "=" in s:
                new_types.append(s.split("#")[0].strip())
            elif s.startswith("export interface "):
                new_types.append(s.split("{")[0].strip())
        
        for t in new_types:
            if t not in patched:
                if "Existing type definitions" not in patched:
                    patched += "\n\nExisting type definitions (MUST preserve these):"
                patched += "\n  " + path + ": " + t
        
        # Extract local imports
        new_imports = []
        for line in content_text.split("\n"):
            s = line.strip()
            if s.startswith("from " + Q + "@/"):
                mod = s.split(Q)[1] if Q in s else ""
                if mod.startswith("@/"):
                    new_imports.append(mod[2:])
            elif s.startswith("from " + Q + "./") or s.startswith("from " + Q + "../"):
                mod = s.split(Q)[1] if Q in s else ""
                new_imports.append(mod)
        
        filtered = [m for m in new_imports if m and "react" not in m]
        for m in filtered:
            if m not in patched:
                if "Local modules imported:" not in patched:
                    patched += "\n\nLocal modules imported: "
                if "Local modules imported:" in patched:
                    patched = patched.rstrip() + ", " + m
        
        # Python requirements
        if path == "requirements.txt":
            for line in content_text.split("\n"):
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("-"):
                    pkg = s.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
                    if pkg and pkg not in patched:
                        if "Installed packages:" in patched:
                            patched = patched.rstrip() + ", " + pkg
                        elif "NEVER rewrite requirements.txt" in patched:
                            patched += "\nInstalled packages: " + pkg
        
        if patched != lines:
            project.context_cache = patched
            from saasclaw_engine.projects.models import Project
            Project.objects.filter(id=project.id).update(context_cache=patched)
    
    elif tool_name == "replace_in_file":
        path = tool_args.get("path", "")
        edits = tool_args.get("edits", [])
        if not path or not edits:
            return
        lines = project.context_cache
        patched = lines
        Q = chr(34)
        
        new_types = []
        new_imports = []
        for edit in edits:
            replace_text = edit.get("replace", "")
            for line in replace_text.split("\n"):
                s = line.strip()
                if s.startswith("type ") and "=" in s:
                    new_types.append(s.split("#")[0].strip().rstrip("{"))
                elif s.startswith("interface ") and "{" in s:
                    new_types.append(s.split("{")[0].strip().rstrip(":"))
                elif s.startswith("from " + Q + "@/"):
                    mod = s.split(Q)[1] if Q in s else ""
                    if mod.startswith("@/"):
                        new_imports.append(mod[2:])
        
        for t in new_types:
            if t not in patched:
                if "Existing type definitions" not in patched:
                    patched += "\n\nExisting type definitions (MUST preserve these):"
                patched += "\n  " + path + ": " + t
        for m in new_imports:
            if m and m not in patched and "react" not in m:
                if "Local modules imported:" not in patched:
                    patched += "\n\nLocal modules imported: "
                if "Local modules imported:" in patched:
                    patched = patched.rstrip() + ", " + m
        
        if patched != lines:
            project.context_cache = patched
            from saasclaw_engine.projects.models import Project
            Project.objects.filter(id=project.id).update(context_cache=patched)



def _scan_codebase_context(workspace_path: str) -> str:
    """Scan the existing codebase to build a framework-agnostic project context."""
    hints = []
    Q = chr(34)  # double quote

    def _read_file(rel_path, max_lines=80):
        fp = os.path.join(workspace_path, rel_path)
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp, errors="replace") as f:
                result = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    result.append(line.rstrip())
                return result
        except Exception:
            return None

    def _read_package_json():
        fp = os.path.join(workspace_path, "package.json")
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp) as f:
                return json.load(f)
        except Exception:
            return None

    # --- Phase 1: Identify project type ---
    top_files = set()
    try:
        top_files = set(os.listdir(workspace_path))
    except Exception:
        pass

    project_type = "unknown"
    pkg = _read_package_json()
    deps = {}
    dev_deps = {}
    scripts = {}
    if pkg:
        deps = pkg.get("dependencies", {})
        dev_deps = pkg.get("devDependencies", {})
        scripts = pkg.get("scripts", {})
        if "next" in deps or "next" in dev_deps:
            project_type = "Next.js"
        elif "react" in deps or "react" in dev_deps:
            project_type = "React"
        elif "vite" in deps or "vite" in dev_deps:
            project_type = "Vite"
        elif "express" in deps:
            project_type = "Express"
        elif os.path.exists(os.path.join(workspace_path, "hugo.toml")):
            project_type = "Hugo"
        elif "package.json" in top_files:
            project_type = "Node.js"
    if "manage.py" in top_files:
        project_type = "Django"
    elif "app.py" in top_files:
        project_type = "Flask"
    elif "Cargo.toml" in top_files:
        project_type = "Rust"
    elif "go.mod" in top_files:
        project_type = "Go"
    elif "requirements.txt" in top_files or "pyproject.toml" in top_files:
        project_type = "Python"

    hints.append(f"Project type: {project_type}")

    # --- Phase 2: Extract build/test commands ---
    build_cmd = None
    test_cmd = None
    if scripts:
        if "build" in scripts:
            build_cmd = "npm run build"
        if "test" in scripts:
            test_cmd = "npm test"
        if "dev" in scripts:
            hints.append("Dev server: npm run dev")
    if project_type == "Django":
        test_cmd = "python manage.py test --settings=config.test_settings"
        hints.append("Dev server: python manage.py runserver")
    elif project_type == "Flask":
        test_cmd = "pytest"
    elif project_type == "Rust":
        build_cmd = "cargo build"
        test_cmd = "cargo test"
    elif project_type == "Go":
        test_cmd = "go test ./..."
    elif project_type == "Hugo":
        build_cmd = "hugo"

    if build_cmd:
        hints.append(f"Build command: {build_cmd}")
    if test_cmd:
        hints.append(f"Test command: {test_cmd}")

    # --- Phase 3: Source structure ---
    source_dirs = []
    for d in ["src", "lib", "app", "components", "pages", "routes", "services",
              "models", "handlers", "api", "utils", "helpers", "templates",
              "static", "public", "config", "types", "interfaces"]:
        full = os.path.join(workspace_path, d)
        if os.path.isdir(full):
            source_dirs.append(d)
    if source_dirs:
        hints.append(f"Source directories: {', '.join(source_dirs)}")

    # --- Phase 4: Scan source files for types and imports ---
    import glob as _glob
    source_files = []
    for ext in ["*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.go", "*.rs"]:
        source_files.extend(_glob.glob(os.path.join(workspace_path, ext)))
        for d in source_dirs:
            source_files.extend(_glob.glob(os.path.join(workspace_path, d, "**", ext), recursive=True))
    source_files = source_files[:40]

    type_defs = []
    local_imports = []
    for fp in source_files:
        rel = os.path.relpath(fp, workspace_path)
        try:
            with open(fp, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("type ") and "=" in line:
                        type_defs.append(rel + ": " + line.split("#")[0].strip().rstrip("{"))
                    elif line.startswith("interface ") and "{" in line:
                        type_defs.append(rel + ": " + line.split("{")[0].strip().rstrip(":"))
                    elif line.startswith("export type ") and "=" in line:
                        type_defs.append(rel + ": " + line.split("#")[0].strip())
                    elif line.startswith("export interface "):
                        type_defs.append(rel + ": " + line.split("{")[0].strip())
                    # Local imports
                    elif line.startswith("from " + Q + "@/"):
                        mod = line.split(Q)[1] if Q in line else ""
                        if mod.startswith("@/"):
                            local_imports.append(mod[2:])
                    elif line.startswith("from " + Q + "./") or line.startswith("from " + Q + "../"):
                        mod = line.split(Q)[1] if Q in line else ""
                        local_imports.append(mod)
        except Exception:
            continue

    if type_defs:
        hints.append("")
        hints.append("Existing type definitions (MUST preserve these):")
        for td in sorted(set(type_defs))[:15]:
            hints.append(f"  {td}")

    if local_imports:
        filtered = sorted(set(m for m in local_imports if m and not m.startswith("react")))
        if filtered:
            hints.append("")
            hints.append(f"Local modules imported: {', '.join(filtered[:20])}")

    # --- Phase 5: Django-specific scan ---
    if project_type == "Django":
        settings_file = None
        for cand in ["config/settings.py", "project/settings.py"]:
            if os.path.exists(os.path.join(workspace_path, cand)):
                settings_file = cand
                break
        if settings_file:
            settings_lines = _read_file(settings_file)
            if settings_lines:
                installed_apps = []
                in_apps = False
                for sl in settings_lines:
                    if "INSTALLED_APPS" in sl:
                        in_apps = True
                        continue
                    if in_apps:
                        if "]" in sl:
                            break
                        app = sl.strip().strip(",'\" ")
                        if app and not app.startswith("#"):
                            installed_apps.append(app)
                if installed_apps:
                    hints.append("")
                    hints.append(f"INSTALLED_APPS ({len(installed_apps)}): {', '.join(installed_apps)}")
                    hints.append("NEVER rewrite INSTALLED_APPS - use replace_in_file to add entries.")

        req_file = _read_file("requirements.txt")
        if req_file:
            pkgs = [l.split("[")[0].split("=")[0].split(">")[0].strip() for l in req_file
                     if l.strip() and not l.startswith("#") and not l.startswith("-")]
            hints.append("")
            hints.append(f"Installed packages: {', '.join(pkgs[:15])}")
            hints.append("NEVER rewrite requirements.txt - use replace_in_file to add deps.")

        hints.append("")
        hints.append("Testing: use SQLite in-memory, NOT from /srv/saasclaw/projects/")
        hints.append("")
        hints.append("Architecture: keep views.py thin (request/response only).")
        hints.append("Business logic → services.py or services/ package (one service per domain).")
        hints.append("Complex queries → model methods or managers, not inline ORM in views.")
        hints.append("Forms/validation → forms.py.")
        hints.append("Permissions/authorization → policies.py or policies/ package.")
        hints.append("Never put imports, helpers, or business logic at the top of views.py.")
        hints.append("")
        hints.append("File size limits (CRITICAL):")
        hints.append("- NEVER create files over 500 lines. Split if growing past 500.")
        hints.append("- models.py > 300 lines → split into models/ package.")
        hints.append("- views.py > 200 lines → split into views/ package.")
        hints.append("- services.py > 200 lines → convert to services/ package.")
        hints.append("")
        hints.append("Django conventions:")
        hints.append("- Views: parse request → call service → return response. No ORM in views.")
        hints.append("- Services: all business logic and side effects. Raise exceptions for errors.")
        hints.append("- Policies: permission checks (can_user_edit, can_deploy, etc.). Import in views.")
        hints.append("- Models: data + relationships only. Custom managers for table-level queries.")
        hints.append("- Tests: tests/test_models.py, tests/test_views.py, tests/test_services.py.")
        hints.append("- Never import models across apps directly — use service layer.")

    # --- Phase 5b: Next.js/React architecture conventions ---
    if project_type in ("Next.js", "React", "Vite", "Express"):
        hints.append("")
        hints.append("Architecture: keep route handlers and pages thin.")
        hints.append("")
        hints.append("File size limits (CRITICAL):")
        hints.append("- NEVER create files over 500 lines. Split if growing past 500.")
        hints.append("- page.tsx/page.jsx: keep under 150 lines. Extract logic to lib/, components/, hooks/.")
        hints.append("- A monolithic page.tsx with embedded game/app logic is NEVER acceptable.")
        hints.append("- React components: one per file. If over 200 lines, extract sub-components.")
        hints.append("")
        hints.append("Next.js/React conventions:")
        hints.append("- src/app/page.tsx → thin shell, imports and composes components. No business logic.")
        hints.append("- src/components/ → one component per file (GameBoard.tsx, not index.tsx)")
        hints.append("- src/lib/ or src/services/ → game rules, API calls, business logic, data transforms")
        hints.append("- src/hooks/ → custom hooks (useGameState, usePlayer, etc.)")
        hints.append("- src/types/ → shared TypeScript interfaces and types")
        hints.append("- src/app/api/<name>/route.ts → one concern per route, under 100 lines")
        hints.append("- State: useReducer for complex state. Lift to parent, pass via props.")
        hints.append("- NEVER inline all game/app logic in a single useState in page.tsx.")
        hints.append("- Extract reusable logic into custom hooks so components stay declarative.")
        hints.append("")
        hints.append("File splitting rules:")
        hints.append("- New game/feature: src/components/GameName.tsx + src/lib/gameName.ts + import in page.tsx")
        hints.append("- If page.tsx is already 150+ lines: REFACTOR by extracting to lib/ and components/ BEFORE adding more.")
        hints.append("- Multiple sub-views: create src/components/GameName/ directory with separate files.")

    # --- Phase 5c: Go conventions ---
    if project_type == "Go":
        hints.append("")
        hints.append("Architecture: keep handlers thin.")
        hints.append("Business logic → internal/ or service/ package.")
        hints.append("Data access → store/ or repository package.")
        hints.append("File size: never exceed 500 lines. Split handlers, services, models into separate files.")

    # --- Phase 5d: Rust conventions ---
    if project_type == "Rust":
        hints.append("")
        hints.append("Architecture: keep main.rs / handlers thin.")
        hints.append("Business logic → separate modules.")
        hints.append("Data access → model or repository modules.")
        hints.append("File size: never exceed 500 lines. Split into modules by concern.")

    # --- Phase 6: Key files listing ---
    key_files = []
    skip = {"node_modules", "__pycache__", ".next", ".git", "dist", "build", ".venv"}
    for name in sorted(top_files):
        if name.startswith("."):
            continue
        if name in skip:
            continue
        if os.path.isdir(os.path.join(workspace_path, name)):
            key_files.append(name + "/")
        else:
            key_files.append(name)
    if key_files:
        hints.append("")
        hints.append(f"Files in root: {', '.join(key_files)}")

    return "\n".join(hints)


# ---------------------------------------------------------------------------
# OpenAI-compatible call (Z.ai, OpenAI, local)
# ---------------------------------------------------------------------------

def _call_openai(config: dict, messages: list[dict], tools: list[dict] = None) -> dict:
    """Call an OpenAI-compatible API. Returns {"content": str, "tool_calls": list | None}."""
    # Z.ai vision models need the standard API endpoint, not the coding plan one
    base_url = config["base_url"]
    if config.get("provider") == "zai" and "v" in config.get("model", "").lower():
        base_url = base_url.replace("/coding/paas/v4", "/paas/v4")

    # OpenAI's newer models (GPT-5.x+) use max_completion_tokens instead of max_tokens
    max_tokens_key = "max_completion_tokens" if config["provider"] == "openai" else "max_tokens"
    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.3,
        max_tokens_key: 50000,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    data = json.dumps(payload).encode("utf-8")
    max_retries = 6

    for attempt in range(max_retries):
        req = urllib_request.Request(
            f"{base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config['api_key']}",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8")[:500]
            except Exception:
                pass

            # Check if this is a plan/access error (not a rate limit)
            is_access_error = exc.code in (403, 429) and any(
                kw in error_body.lower()
                for kw in ["subscription", "plan", "access", "not included", "does not"]
            )
            if is_access_error:
                raise RuntimeError(
                    f"Model '{config['model']}' is not available on your current plan. "
                    f"Switch to a different model in the dropdown. Error: {error_body}"
                )

            # Check if this is the max_tokens parameter error -- retry with the correct key
            if exc.code == 400 and "max_tokens" in error_body and "max_completion_tokens" in error_body:
                payload.pop("max_tokens", None)
                payload["max_completion_tokens"] = 50000
                data = json.dumps(payload).encode("utf-8")
                continue

            if exc.code == 429 and attempt < max_retries - 1 and not is_access_error:
                wait = 2 ** attempt * 3
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = min(int(retry_after), 60)
                    except ValueError:
                        pass
                time.sleep(wait)
                continue
            raise RuntimeError(f"LLM API error {exc.code}: {error_body}")
        except URLError as exc:
            raise RuntimeError(f"LLM API request failed: {exc}")
        except json.JSONDecodeError:
            raise RuntimeError("LLM API returned invalid JSON.")
    else:
        raise RuntimeError("LLM API: max retries exceeded after rate limiting.")

    choice = body.get("choices", [{}])[0].get("message", {})
    usage = body.get("usage", {})
    return {
        "content": choice.get("content") or "",
        "tool_calls": choice.get("tool_calls"),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        } if usage else None,
    }


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------

def _call_anthropic(config: dict, messages: list[dict], tools: list[dict] = None) -> dict:
    """Call Anthropic's Messages API. Returns {"content": str, "tool_calls": list | None}."""
    # Extract system message
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text += m["content"] + "\n"
        else:
            content = m["content"]
            # Convert OpenAI-style multipart content to Anthropic format
            if isinstance(content, list):
                anthropic_content = []
                for block in content:
                    if block.get("type") == "text":
                        anthropic_content.append({"type": "text", "text": block["text"]})
                    elif block.get("type") == "image_url":
                        # Extract base64 data from data URL
                        url = block["image_url"]["url"]
                        # data:image/png;base64,...
                        header, _, b64data = url.partition(",")
                        mime = "image/png"
                        if ":" in header and ";" in header:
                            mime = header.split(":")[1].split(";")[0]
                        anthropic_content.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": b64data},
                        })
                api_messages.append({"role": m["role"], "content": anthropic_content})
            else:
                api_messages.append({"role": m["role"], "content": content})

    # Convert OpenAI tool defs to Anthropic format
    anthropic_tools = None
    if tools:
        anthropic_tools = []
        for t in tools:
            fn = t.get("function", t)
            anthropic_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

    payload = {
        "model": config["model"],
        "max_tokens": 50000,
        "system": system_text.strip(),
        "messages": api_messages,
    }
    if anthropic_tools:
        payload["tools"] = anthropic_tools

    # Enable prompt caching for system prompt + tools
    payload["system"] = [
        {"type": "text", "text": system_text.strip(), "cache_control": {"type": "ephemeral"}}
    ]

    data = json.dumps(payload).encode("utf-8")
    max_retries = 6

    for attempt in range(max_retries):
        req = urllib_request.Request(
            f"{config['base_url']}/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": config["api_key"],
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as exc:
            if exc.code == 429 and attempt < max_retries - 1:
                wait = 2 ** attempt * 3
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = min(int(retry_after), 60)
                    except ValueError:
                        pass
                time.sleep(wait)
                continue
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise RuntimeError(f"Anthropic API error {exc.code}: {error_body}")
        except URLError as exc:
            raise RuntimeError(f"Anthropic API request failed: {exc}")
        except json.JSONDecodeError:
            raise RuntimeError("Anthropic API returned invalid JSON.")
    else:
        raise RuntimeError("Anthropic API: max retries exceeded after rate limiting.")

    # Parse Anthropic response → normalize to our format
    content_blocks = body.get("content", [])
    text_parts = []
    tool_use_blocks = []

    for block in content_blocks:
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_use_blocks.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    return {
        "content": "\n".join(text_parts),
        "tool_calls": tool_use_blocks if tool_use_blocks else None,
    }


# ---------------------------------------------------------------------------
# Unified dispatch
# ---------------------------------------------------------------------------

def _call_llm(messages: list[dict], tools: list[dict] = None, provider: str = None, model: str = None, user=None) -> dict:
    """Call the active LLM provider and return {"content": str, "tool_calls": list | None}."""
    config = _provider_config(session_override=provider, model_override=model, user=user)

    # Providers that don't support multipart content (image_url blocks)
    # Z.ai supports vision but only on GLM-4V/4.5V models, not the default text model
    supports_vision = False
    if config["provider"] == "openai" or config["format"] == "anthropic":
        supports_vision = True
    elif config["provider"] == "zai" and "V" in config.get("model", ""):
        supports_vision = True

    if not supports_vision:
        # Flatten any multipart content to plain text
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        text_parts.append("[image attached -- current model does not support vision. Switch to a vision-capable model to analyze images.]")
                    elif isinstance(block, str):
                        text_parts.append(block)
                m["content"] = "\n".join(text_parts)

    if config["format"] == "anthropic":
        return _call_anthropic(config, messages, tools)
    else:
        return _call_openai(config, messages, tools)


def _call_llm_stream(messages: list[dict], tools: list[dict] = None, provider: str = None, model: str = None, user=None):
    """Stream an OpenAI-compatible LLM call. Yields (delta_text, tool_calls_delta)."""
    config = _provider_config(session_override=provider, model_override=model, user=user)

    # Flatten multipart content for non-vision providers (same as _call_llm)
    supports_vision = False
    if config["provider"] == "openai" or config["format"] == "anthropic":
        supports_vision = True
    elif config["provider"] == "zai" and "V" in config.get("model", ""):
        supports_vision = True
    if not supports_vision:
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif isinstance(block, str):
                        text_parts.append(block)
                m["content"] = "\n".join(text_parts)

    if config["format"] == "anthropic":
        # Anthropic streaming is different -- fall back to non-streaming and yield all at once
        result = _call_anthropic(config, messages, tools)
        yield result["content"], result.get("tool_calls")
        return

    base_url = config["base_url"]
    if config.get("provider") == "zai" and "v" in config.get("model", "").lower():
        base_url = base_url.replace("/coding/paas/v4", "/paas/v4")

    max_tokens_key = "max_completion_tokens" if config["provider"] == "openai" else "max_tokens"
    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.3,
        max_tokens_key: 50000,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )

    try:
        resp = urllib_request.urlopen(req, timeout=300)
    except HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise RuntimeError(f"LLM API error {exc.code}: {error_body}")
    except URLError as exc:
        raise RuntimeError(f"LLM API request failed: {exc}")

    # Parse SSE stream from the API
    accumulated_tool_calls = {}
    usage_data = None
    for line in resp:
        line = line.decode("utf-8").strip()
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if chunk.get("usage"):
            usage_data = chunk["usage"]
        delta = chunk.get("choices", [{}])[0].get("delta", {})
        text_delta = delta.get("content", "")
        tc_delta = delta.get("tool_calls")
        if tc_delta:
            for tc in tc_delta:
                idx = tc.get("index", 0)
                if idx not in accumulated_tool_calls:
                    accumulated_tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                if tc.get("id"):
                    accumulated_tool_calls[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    accumulated_tool_calls[idx]["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    accumulated_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
        if text_delta:
            yield text_delta, None

    if usage_data:
        _last_usage.update(usage_data)

    # Yield final accumulated tool calls
    if accumulated_tool_calls:
        tool_list = [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls)]
        yield "", tool_list

    resp.close()


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(
    workspace_path: str,
    project_name: str,
    conversation: list[dict],
    user_message: str,
    provider: str = None,
    images: list[dict] = None,
    model: str = None,
    user=None,
    project_notes: str = '',
    project_directives: str = '',
    profile_prompt: str = '', profile_tools: list = None,
    project_context: str = "",
    project_id: int = None,
    session_id: str = None,
    project_todos: list = None,
) -> list[dict]:
    """Run the agent loop.

    Args:
        workspace_path: Filesystem path to the git workspace.
        project_name: Display name of the project.
        conversation: Previous messages [{"role": ..., "content": ...}].
        user_message: The new user message.
        images: Optional list of {"data": base64_str, "mime": "image/png"} dicts.

    Returns:
        List of new messages: [{"role": ..., "content": ..., "tool_call": ...}]
        The last message is the assistant's final response.
    """
    system = {"role": "system", "content": _system_prompt(workspace_path, project_name, project_notes, project_directives, profile_prompt, profile_tools, project_todos=project_todos, project_context=project_context)}
    messages = [system]
    messages.extend(_compact_conversation(conversation))

    # Build user message with optional images
    if images:
        user_content = [{"type": "text", "text": user_message}]
        for img in images:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img.get('mime', 'image/png')};base64,{img['data']}"},
            })

        # Scan multimodal content for prompt injection
        from saasclaw_engine.agent.prompt_guard import scan_multimodal_content
        scan = scan_multimodal_content(user_message, images, source=f"runner:{project_name}")
        if not scan["allowed"]:
            logger.warning("Prompt injection blocked in run_agent for project %s (severity=%s)",
                           project_name, scan["severity"])
            return [{"role": "assistant", "content": f"I detected potentially unsafe input and can't process that request. If you believe this is an error, try rephrasing your message.", "tool_call": {}}]

        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_message})

    # For the stored message log, note that images were attached
    display_content = user_message
    if images:
        display_content += f"\n[{len(images)} image(s) attached]"

    new_messages = [{"role": "user", "content": display_content, "tool_call": {}}]
    # Filter tools based on profile
    if profile_tools:
        tools = [t for t in agent_tools.TOOL_DEFINITIONS if t['function']['name'] in profile_tools]
    else:
        tools = agent_tools.TOOL_DEFINITIONS

    # Track tool output sizes for context management
    TOOL_OUTPUT_OVERHEAD = 1500  # chars; truncate old tool results to this
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _trim_context(msgs: list[dict]):
        """Truncate large tool results in older messages to save tokens."""
        # Keep the last 6 messages intact, trim older ones
        if len(msgs) <= 8:
            return
        for i in range(2, len(msgs) - 6):  # skip system + first user, keep last 6
            m = msgs[i]
            if m.get("role") == "tool":
                content = m.get("content", "")
                if len(content) > TOOL_OUTPUT_OVERHEAD:
                    m["content"] = content[:TOOL_OUTPUT_OVERHEAD] + "\n... (truncated for context)"

    tool_call_count = 0
    consecutive_errors = 0  # Track repeated identical failures
    last_error_key = None
    for round_num in range(MAX_TOOL_ROUNDS):
        _trim_context(messages)
        if session_id:
            set_agent_activity(session_id, "Thinking…" if round_num == 0 else "Figuring out next step…", tool_call_count)

        # Efficiency: inject a wrap-up hint if the model is taking too many steps
        if tool_call_count >= EFFICIENCY_WARNING_THRESHOLD:
            messages.append({
                "role": "system",
                "content": "You've made many tool calls. Wrap up the current task immediately — commit what you have and stop. Don't start new work.",
            })
            logger.info("Efficiency warning injected at round %d (tool_calls=%d)", round_num, tool_call_count)

        # Cost check: force stop if budget exceeded
        if total_usage["total_tokens"] > 0:
            est = _estimate_cost(provider or os.environ.get("STUDIO_LLM_PROVIDER", "zai"), model or os.environ.get("STUDIO_MODEL", "glm-5.1"), total_usage["prompt_tokens"], total_usage["completion_tokens"])
            if est > MAX_TOOL_COST_PER_TURN:
                msg_text = f"⚠️ Reached ${est:.2f} spend limit for this turn. Committing progress and stopping."
                new_messages.append({"role": "assistant", "content": msg_text, "tool_call": {}})
                logger.warning("Cost limit hit: $%.2f at round %d", est, round_num)
                break

        # Sanitize messages before sending to LLM (redact PII)
        messages, _pii_redactions = sanitize_messages(messages)
        if _pii_redactions:
            logger.warning("PII redacted %d pattern(s) before LLM call in round %d", len(_pii_redactions), round_num)

        result = _call_llm(messages, tools, provider=provider, model=model, user=user)

        content = result["content"]
        tool_calls = result["tool_calls"]
        
        # Accumulate token usage
        if result.get("usage"):
            total_usage["prompt_tokens"] += result["usage"].get("prompt_tokens", 0)
            total_usage["completion_tokens"] += result["usage"].get("completion_tokens", 0)
            total_usage["total_tokens"] += result["usage"].get("total_tokens", 0)

        if not tool_calls:
            if content:
                # Strip model echoing the user's message at the start
                if user_message and isinstance(user_message, str) and content.startswith(user_message):
                    content = content[len(user_message):].lstrip('\n')
                # Auto-continue if model fakes a "tool limit" response (GLM quirk)
                if "tool limit" in content.lower():
                    logger.info("Model generated fake tool-limit response, showing to user")
                new_messages.append({"role": "assistant", "content": content, "tool_call": {}})
            else:
                new_messages.append({
                    "role": "assistant",
                    "content": "I wasn't able to generate a response.",
                    "tool_call": {},
                })
            break

        # If agent asks permission but also has tool calls, strip the question
        # (the tool calls ARE the agent proceeding)
        if content and tool_calls:
            content = _strip_permission_question(content, has_tool_calls=True)

        # Assistant wants to call tools
        assistant_msg = {"role": "assistant", "content": content or ""}
        assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        first_call = tool_calls[0].get("function", {})
        new_messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_call": {
                "name": first_call.get("name", ""),
                "args": first_call.get("arguments", ""),
            },
        })

        # Execute tool calls in parallel
        def _exec_single(tc):
            """Execute a single tool call, returning (tc, name, raw_args, args, tool_result)."""
            func = tc.get("function", {})
            name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, ValueError):
                # Try to repair common streaming issues (truncated JSON, raw newlines)
                if isinstance(raw_args, str) and len(raw_args) > 10:
                    try:
                        repaired = raw_args.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
                        args = json.loads(repaired)
                    except (json.JSONDecodeError, ValueError):
                        try:
                            args = json.loads(raw_args, strict=False)
                        except (json.JSONDecodeError, ValueError):
                            args = {}
                else:
                    args = {}
            tool_result = agent_tools.execute_tool(workspace_path, name, args, bool(profile_tools))
            return (tc, name, raw_args, args, tool_result)

        # Filter tool calls that fit within the budget
        eligible_calls = [tc for tc in tool_calls if tool_call_count < MAX_TOTAL_TOOL_CALLS]
        tool_call_count += len(eligible_calls)

        if len(eligible_calls) > 1:
            # Run concurrently
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(_exec_single, tc): tc for tc in eligible_calls}
                results_in_order = []
                for future in concurrent.futures.as_completed(futures):
                    results_in_order.append(future.result())
            # Sort by original index for conversation history
            results_in_order.sort(key=lambda x: eligible_calls.index(x[0]))
        else:
            # Single call — no thread overhead
            results_in_order = [_exec_single(tc) for tc in eligible_calls]

        # Process results (preserving all UI/activity/context behavior)
        for tc, name, raw_args, args, tool_result in results_in_order:
            if session_id:
                args_preview = args
                friendly = {"read_file": "Reading", "write_file": "Writing", "replace_in_file": "Editing", "list_files": "Listing files", "git_status": "Checking git", "git_commit": "Committing", "git_diff": "Viewing diff", "run_command": "Running command", "web_fetch": "Fetching", "web_search": "Searching"}
                display = friendly.get(name, name)
                if name in ("read_file", "write_file", "replace_in_file") and isinstance(args_preview, dict):
                    display += f" {args_preview.get('path', '...')}"
                elif name == "run_command" and isinstance(args_preview, dict):
                    cmd = args_preview.get('command', '')[:60]
                    display += f" `{cmd}`"
                set_agent_activity(session_id, display, tool_call_count)

            # Break on repeated identical tool errors
            if tool_result.startswith("Error:"):
                error_key = (name, raw_args if isinstance(raw_args, str) else str(raw_args))
                if error_key == last_error_key:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 1
                    last_error_key = error_key
                if consecutive_errors >= 3:
                    msg_text = f"⚠️ Stuck on repeated error with {name}: {tool_result[:200]}"
                    new_messages.append({"role": "assistant", "content": msg_text, "tool_call": {}})
                    break
            else:
                consecutive_errors = 0
                last_error_key = None

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result,
            })

            # Log tool args for debugging (skip full content to avoid bloat)
            logged_args = dict(args) if isinstance(args, dict) else {}
            if 'content' in logged_args and len(str(logged_args['content'])) > 100:
                logged_args['content'] = str(logged_args['content'])[:100] + '...'
            logger.info("Tool call: %s args=%s result_len=%d", name, logged_args, len(tool_result))

            # Patch project context cache for write operations
            if name in ("write_file", "replace_in_file") and project_id:
                _patch_context_on_tool(project_id, name, args, tool_result)

            new_messages.append({
                "role": "tool",
                "content": tool_result,
                "tool_call": {"name": name, "result": tool_result[:200]},
            })

    else:
        new_messages.append({
            "role": "assistant",
            "content": "I'm making progress but hit the turn limit. Type 'continue' and I'll keep going from where I left off.",
            "tool_call": {},
        })

    # Append usage info as a special message for the caller to extract
    if total_usage["total_tokens"] > 0:
        new_messages.append({"role": "_usage", "content": "", "tool_call": {}, "usage": total_usage})

    if session_id:
        clear_agent_activity(session_id)

    return new_messages


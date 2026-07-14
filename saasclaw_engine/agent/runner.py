"""Agent runner — the core agentic loop.

Receives a user message, calls an LLM, executes any requested tools,
feeds results back, and returns the final response.

Extracted modules:
- activity.py — thread-safe agent activity tracking
- llm_backends.py — LLM provider configs, pricing, API calls
- prompt_utils.py — system prompt building, conversation compaction
- context_scan.py — codebase scanning and incremental context patching
"""
import json
import logging
import os
import subprocess
import time
import concurrent.futures

from django.conf import settings

from . import tools as agent_tools
from .pii_guard import sanitize_messages
from .activity import get_agent_activity, set_agent_activity, clear_agent_activity
from .llm_backends import (
    LLM_PRICING, AVAILABLE_MODELS,
    _estimate_cost, _provider_config,
    _call_openai, _call_anthropic, _call_llm, _call_llm_stream,
    MAX_TOOL_ROUNDS, MAX_TOTAL_TOOL_CALLS, MAX_TOOL_COST_PER_TURN,
    EFFICIENCY_WARNING_THRESHOLD,
)
from .prompt_utils import (
    _system_prompt, _compact_conversation,
    _is_permission_question, _strip_permission_question,
)
from .context_scan import (
    _scan_project_files, _patch_context_on_tool,
    _do_patch_context, _scan_codebase_context,
)

logger = logging.getLogger(__name__)


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
    # Filter tools based on profile and platform settings
    if profile_tools:
        tools = [t for t in agent_tools.TOOL_DEFINITIONS if t['function']['name'] in profile_tools]
    else:
        tools = agent_tools.TOOL_DEFINITIONS

    # Strip web_search unless platform setting allows it
    try:
        from saasclaw_engine.studio_models.models import SiteSettings
        if not SiteSettings.get().wizard_web_search_enabled:
            tools = [t for t in tools if t['function']['name'] != 'web_search']
    except Exception:
        pass

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
                msg_text = "⚠️ Token limit reached for this turn. Committing progress and stopping."
                new_messages.append({"role": "assistant", "content": msg_text, "tool_call": {}})
                logger.warning("Token limit hit at round %d", round_num)
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
                # Model asks permission without acting -- re-prompt it to proceed
                if _is_permission_question(content) and round_num == 0:
                    logger.info("Model asked permission without tool calls, re-prompting to proceed")
                    new_messages.append({"role": "assistant", "content": content, "tool_call": {}})
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "Yes, proceed. Execute the changes now."})
                    continue
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
            tool_result = agent_tools.execute_tool(workspace_path, name, args, bool(profile_tools), session_id=session_id)
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

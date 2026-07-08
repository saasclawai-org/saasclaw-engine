"""LLM provider backends and pricing.

Supports multiple LLM backends:
  - "zai"       → Z.ai GLM (default, OpenAI-compatible)
  - "openai"    → OpenAI GPT
  - "anthropic" → Anthropic Claude
  - "local"     → Local llama.cpp server

All OpenAI-compatible backends share one code path.
Anthropic uses its own message format.
"""
import json
import logging
import os
import time
import concurrent.futures
from urllib import request as urllib_request
from urllib.error import URLError, HTTPError

from django.conf import settings

logger = logging.getLogger(__name__)


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
    }

    return configs.get(provider, configs["zai"])
_last_usage = {}


MAX_TOOL_ROUNDS = 30  # Cap LLM round-trips per turn (was 100)
MAX_TOTAL_TOOL_CALLS = 60  # Hard cap on total tool calls per turn (was 300)
MAX_TOOL_COST_PER_TURN = 0.50  # $0.50 USD per turn before forced stop (Zai pricing)
EFFICIENCY_WARNING_THRESHOLD = 12  # Warn the model to wrap up after this many calls


# ---------------------------------------------------------------------------
# Provider configurations
# ---------------------------------------------------------------------------
AVAILABLE_MODELS = {
    "zai": [
        {"model": "glm-5-turbo", "label": "GLM-5 Turbo (fast, cheap)", "vision": False},
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

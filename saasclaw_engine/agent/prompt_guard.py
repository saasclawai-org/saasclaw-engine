"""
Prompt injection guard for SaaSClaw.

Uses the sunglasses library (https://github.com/sunglasses-dev/sunglasses)
to scan user input and image content before sending to LLM providers.

Provides:
- scan_user_input(): scan text for prompt injection patterns
- scan_image(): scan OCR text from images
- log_blocked(): audit log blocked attempts
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("saasclaw.prompt_guard")

_engine = None
_log_path = Path("/srv/saasclaw/logs/prompt-guard.log")


def _get_engine():
    """Lazy-load the sunglasses engine."""
    global _engine
    if _engine is None:
        try:
            from sunglasses.engine import SunglassesEngine
            _engine = SunglassesEngine()
            logger.info("Sunglasses prompt injection guard loaded")
        except ImportError:
            logger.warning("sunglasses not installed — prompt injection scanning disabled")
            _engine = False
    return _engine


def scan_user_input(text: str, source: str = "wizard") -> dict:
    """Scan user text for prompt injection patterns.

    Args:
        text: User-provided text input.
        source: Origin label for logging (e.g., "wizard", "gateway", "form-api").

    Returns:
        dict with keys:
            - allowed (bool): True if the input is safe to send to the LLM.
            - decision (str): "allow" or "block".
            - severity (str): "clean", "low", "medium", "high", "critical".
            - findings (list): Matched threat descriptions.
            - latency_ms (float): Scan time in milliseconds.
            - cleaned (str): If blocked, the findings summary for the user.
    """
    engine = _get_engine()
    if engine is False:
        # sunglasses not installed — allow everything but log
        return {"allowed": True, "decision": "allow", "severity": "unknown",
                "findings": [], "latency_ms": 0, "cleaned": text}

    if not text or not text.strip():
        return {"allowed": True, "decision": "allow", "severity": "clean",
                "findings": [], "latency_ms": 0, "cleaned": text}

    start = time.perf_counter()
    result = engine.scan(text)
    elapsed_ms = (time.perf_counter() - start) * 1000

    response = {
        "allowed": result.is_clean,
        "decision": result.decision,
        "severity": result.severity,
        "findings": [str(f) for f in result.findings] if result.findings else [],
        "latency_ms": round(elapsed_ms, 2),
        "cleaned": text,
    }

    # Filter out false-positive-prone categories for wizard sessions.
    # secret_detection scans reversed text and matches normal phrases like
    # "shown on the homepage" as PEM key patterns.
    if not result.is_clean and result.findings:
        from sunglasses.engine import ScanResult
        filtered = [f for f in result.findings
                    if not (isinstance(f, dict) and f.get("category") == "secret_detection")]
        if not filtered:
            # All findings were false-positive secret detection — allow
            result = ScanResult(is_clean=True, decision="allow", severity="clean", findings=[])
            response = {
                "allowed": True,
                "decision": "allow",
                "severity": "clean",
                "findings": [],
                "latency_ms": elapsed_ms,
                "cleaned": text,
            }
        else:
            response["findings"] = [str(f) for f in filtered]

    if not result.is_clean:
        _log_blocked(source, text, response)

    return response


def scan_multimodal_content(
    text: str,
    images: list[dict],
    source: str = "wizard",
) -> dict:
    """Scan text + image content for prompt injection.

    Args:
        text: User-provided text.
        images: List of {"data": base64, "mime": str} dicts.
        source: Origin label for logging.

    Returns:
        Same dict as scan_user_input. Images with injection patterns
        in OCR-extracted text are flagged.
    """
    text_result = scan_user_input(text, source)

    if not text_result["allowed"]:
        return text_result

    if not images:
        return text_result

    engine = _get_engine()
    if engine is False:
        return text_result

    # Scan images for hidden text via OCR
    for img in images:
        try:
            img_result = engine.scan_image_bytes(img["data"], img["mime"])
            if img_result and not img_result.is_clean:
                text_result["allowed"] = False
                text_result["decision"] = "block"
                text_result["severity"] = img_result.severity or "high"
                text_result["findings"].extend(
                    [f"[image] {f}" for f in (img_result.findings or [])]
                )
        except Exception:
            # OCR not available or unsupported format — skip
            logger.debug("Image scan failed (OCR may not be available)")
            break

    if not text_result["allowed"]:
        _log_blocked(source, f"{text[:100]} [with images]", text_result)

    return text_result


def _log_blocked(source: str, text: str, result: dict):
    """Log a blocked prompt injection attempt."""
    try:
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "severity": result.get("severity", "unknown"),
            "findings": result.get("findings", []),
            "latency_ms": result.get("latency_ms", 0),
            "text_preview": text[:200],
        }
        with open(_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.warning(
            "Prompt injection blocked from %s (severity=%s, findings=%d)",
            source, result.get("severity"), len(result.get("findings", [])),
        )
    except Exception as exc:
        logger.error("Failed to log blocked prompt: %s", exc)

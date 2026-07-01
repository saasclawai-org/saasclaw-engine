"""
PII detection and sanitization for the SaaSClaw agent.

Uses the PII Guard microservice (Presidio) when available, falling back
to built-in regex patterns when the service is unreachable.

Usage:
    from saasclaw_engine.agent.pii_guard import sanitize_for_llm

    clean_text, redaction_log = sanitize_for_llm(raw_text)
    # clean_text goes to the LLM
    # redaction_log tracks what was redacted (for audit/display)
"""

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Service config ───────────────────────────────────────────────────────

_SERVICE_URL = os.getenv("PII_GUARD_URL", "http://127.0.0.1:8900")
_TIMEOUT_S = float(os.getenv("PII_GUARD_TIMEOUT", "2.0"))
_client: Optional[httpx.Client] = None


def _get_client() -> Optional[httpx.Client]:
    """Lazy-init an HTTP client to the PII Guard service."""
    global _client
    if _client is None:
        try:
            _client = httpx.Client(base_url=_SERVICE_URL, timeout=_TIMEOUT_S)
            # Quick health check
            resp = _client.get("/health")
            resp.raise_for_status()
            logger.info("PII Guard service connected at %s", _SERVICE_URL)
        except Exception as e:
            logger.warning("PII Guard service unavailable at %s: %s — using regex fallback", _SERVICE_URL, e)
            _client = None
    return _client


def _service_healthy() -> bool:
    """Check if the service is reachable (no exception = healthy)."""
    client = _get_client()
    if client is None:
        return False
    try:
        resp = client.get("/health", timeout=1.0)
        return resp.status_code == 200
    except Exception:
        return False


# ── Regex fallback (legacy patterns) ──────────────────────────────────────
# Used when the PII Guard service is unavailable.

PATTERNS = [
    # 1. Database connection strings
    (re.compile(
        r'(?i)\b(?:mysql|postgres(?:ql)?|mongodb|redis)://(?:[\w._-]+(?::[^\s@]+)?|:[^\s@]+)@[\w.-]+(?::\d+)?(?:/[\w./-]*)?'
    ), '{{DB_CONN}}', 'Database Connection'),

    # 2. US Social Security Numbers
    (re.compile(
        r'(?<!\d)(?!000|666)\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}(?!\d)'
    ), '{{SSN}}', 'SSN'),

    # 3. Credit card numbers
    (re.compile(
        r'(?<!\d)(?:4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'
        r'|5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'
        r'|3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}'
        r'|6(?:011|5\d{2})[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4})(?!\d)'
    ), '{{CC}}', 'Credit Card'),

    # 4. US phone numbers
    (re.compile(
        r'(?<!\d)\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)'
    ), '({{PHONE}})', 'Phone'),

    # 5. Email addresses
    (re.compile(
        r'\b[A-Za-z0-9._%+-]+@(?!localhost\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
    ), '{{EMAIL}}', 'Email'),

    # 6. US mailing addresses
    (re.compile(
        r'\b\d+\s+[A-Za-z0-9\s,.]+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Rd|Road|Ct|Court|Pl|Place|Way|#\d+)\s*,\s*[A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b'
    ), '{{ADDRESS}}', 'Address'),

    # 7. Bank routing numbers
    (re.compile(
        r'(?i)\b(?:routing|aba|bank[_\s-]*routing)[_\s-]*(?:number|no\.?|#)?[\s"]*:?[\s"]*\d{9}\b'
    ), '{{ROUTING}}', 'Bank Routing'),

    # 8. Bank account numbers
    (re.compile(
        r'(?i)\b(?:account[_\s-]*(?:number|no\.?|#)?)[\s"]*:?\s*"?\d{8,17}\b'
    ), '{{ACCT}}', 'Bank Account'),

    # 9. Salary / compensation
    (re.compile(
        r'(?i)\b(?:salary|annual[_\s-]*salary|base[_\s-]*pay|compensation|hourly[_\s-]*rate|wage|pay[_\s-]*rate)[\s"]*:?\s*"?[\$]?[\d,]+(?:\.\d{2})?(?:\s*(?:per\s*)?(?:year|annum|month|hour|hr))?\b'
    ), '{{SALARY}}', 'Salary'),

    # 10. Dates of birth
    (re.compile(
        r'(?i)(?:date[_\s-]*of[_\s-]*birth|dob|born(?:\s+on)?|birth[_\s-]?date|birthday)[\s":,]*\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}'
    ), '{{DOB}}', 'Date of Birth'),

    # 11. Passport numbers
    (re.compile(
        r'(?i)\b(?:passport[_\s-]?(?:number|no\.?|#|id)?)[\s"]*:?\s*"?[A-Z]?\d{8,9}\b'
    ), '{{PASSPORT}}', 'Passport'),

    # 12. Driver's license numbers
    (re.compile(
        r"""(?i)\b(?:driver(?:'s|\\')?\s*(?:license|lic(?:ense)?)\s*(?:number|no\.?#)?)\s*:?\s*[A-Z]?\d{7,13}\b"""
    ), '{{DL}}', 'Driver License'),

    # 13. IP addresses
    (re.compile(
        r'(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?!\d)'
    ), '{{IP}}', 'IP Address'),

    # 14. AWS access key IDs
    (re.compile(
        r'\bAKIA[0-9A-Z]{16}\b'
    ), '{{AWS_KEY}}', 'AWS Key'),
]


def _load_custom_patterns():
    """Load custom PII patterns from database. Returns list of (regex, placeholder, label) tuples."""
    try:
        from saasclaw_engine.studio_models.models import CustomPiiPattern
        custom = []
        for p in CustomPiiPattern.objects.filter(is_active=True):
            try:
                compiled = re.compile(p.regex)
                custom.append((compiled, p.placeholder, p.name))
            except re.error as e:
                logger.warning("Invalid custom PII pattern '%s': %s", p.name, e)
        return custom
    except Exception as e:
        logger.debug("Could not load custom PII patterns: %s", e)
        return []


def get_active_patterns():
    """Return built-in patterns plus any active custom patterns from DB."""
    return PATTERNS + _load_custom_patterns()


# ── Regex fallback implementations ───────────────────────────────────────

def detect_pii(text: str) -> list[dict]:
    """Scan text and return a list of detected PII matches."""
    # Try service first
    client = _get_client()
    if client is not None:
        try:
            resp = client.post("/analyze", json={"text": text})
            if resp.status_code == 200:
                findings = resp.json()
                if findings:
                    return findings
                # Service healthy but returned nothing — also try regex
        except Exception as e:
            logger.warning("PII Guard service call failed, using regex: %s", e)

    # Regex fallback (also supplements empty service results)
    findings = []
    for regex, placeholder, label in get_active_patterns():
        for m in regex.finditer(text):
            findings.append({
                'label': label,
                'match': m.group(),
                'start': m.start(),
                'end': m.end(),
                'placeholder': placeholder,
            })
    findings.sort(key=lambda f: f['start'])
    return findings


def sanitize_for_llm(text: str, enabled: bool = True) -> tuple[str, list[dict]]:
    """Sanitize text by redacting detected PII patterns.

    Uses the PII Guard service when available, falls back to regex.

    Returns: (sanitized_text, redaction_log)
    """
    if not enabled or not text:
        return text, []

    # Try service first
    client = _get_client()
    if client is not None:
        try:
            resp = client.post("/sanitize", json={"text": text})
            if resp.status_code == 200:
                data = resp.json()
                log = data.get("redactions", [])
                if log:
                    summary = ', '.join(f"{l['label']}({l['placeholder']})" for l in log)
                    logger.info("PII redacted (service): %s", summary)
                    return data["text"], log
        except Exception as e:
            logger.warning("PII Guard service sanitize failed, using regex: %s", e)

    # Regex fallback
    findings = detect_pii(text)
    if not findings:
        return text, []

    claim = [None] * len(text)
    for finding in findings:
        for i in range(finding['start'], min(finding['end'], len(text))):
            if claim[i] is None:
                claim[i] = finding

    result = []
    log = []
    i = 0
    while i < len(text):
        if claim[i] is not None:
            finding = claim[i]
            if i == finding['start']:
                result.append(finding['placeholder'])
                log.append({
                    'label': finding['label'],
                    'placeholder': finding['placeholder'],
                    'original': finding['match'][:50],
                })
            i = finding['end']
        else:
            result.append(text[i])
            i += 1

    if log:
        summary = ', '.join(f"{l['label']}({l['placeholder']})" for l in log)
        logger.info("PII redacted (regex fallback): %s", summary)

    return ''.join(result), log


def sanitize_messages(messages: list[dict], enabled: bool = True) -> tuple[list[dict], list[dict]]:
    """Sanitize all messages in a conversation before sending to LLM.

    Uses the PII Guard service when available, falls back to per-message regex.

    Returns: (sanitized_messages, combined_redaction_log)
    """
    if not enabled or not messages:
        return messages, []

    # Try service first (single batch call)
    client = _get_client()
    if client is not None:
        try:
            resp = client.post("/sanitize/messages", json={"messages": messages})
            if resp.status_code == 200:
                data = resp.json()
                redactions = data.get("redactions", [])
                if redactions:
                    summary = ', '.join(f"{l['label']}({l['placeholder']})" for l in redactions)
                    logger.info("PII redacted (service, batch): %s", summary)
                    return data["messages"], redactions
        except Exception as e:
            logger.warning("PII Guard service batch sanitize failed, using per-message regex: %s", e)

    # Regex fallback: per-message
    all_redactions = []
    sanitized = []

    for msg in messages:
        content = msg.get('content', '')

        if isinstance(content, str):
            clean, redactions = sanitize_for_llm(content, enabled=True)
            if redactions:
                all_redactions.extend(redactions)
            sanitized.append({**msg, 'content': clean})
        elif isinstance(content, list):
            clean_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    clean_text, redactions = sanitize_for_llm(block.get('text', ''), enabled=True)
                    if redactions:
                        all_redactions.extend(redactions)
                    clean_blocks.append({**block, 'text': clean_text})
                else:
                    clean_blocks.append(block)
            sanitized.append({**msg, 'content': clean_blocks})
        else:
            sanitized.append(msg)

    return sanitized, all_redactions

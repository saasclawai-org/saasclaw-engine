"""
PII detection and sanitization for the SaaSClaw agent.

Scans text content before it reaches the LLM and redacts sensitive patterns
(SSNs, credit cards, phone numbers, emails, addresses, financial data, etc.).
Replaces detected values with synthetic placeholders so the agent can still
reason about structure without exposing real data.

Usage:
    from saasclaw_engine.agent.pii_guard import sanitize_for_llm

    clean_text, redaction_log = sanitize_for_llm(raw_text)
    # clean_text goes to the LLM
    # redaction_log tracks what was redacted (for audit/display)
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────────
# Each pattern: (compiled regex, placeholder template, label for logging)

PATTERNS = [
    # US Social Security Numbers: 123-45-6789 or 123 45 6789 or 123456789
    (re.compile(
        r'\b(?!000|666|9\d{2})\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b'
    ), '{{SSN}}', 'SSN'),

    # Credit card numbers (Visa 16, Mastercard 16, Amex 15, with optional dashes/spaces)
    (re.compile(
        r'\b(?:4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'           # Visa
        r'|5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'          # Mastercard
        r'|3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}'                      # Amex
        r'|6(?:011|5\d{2})[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4})\b'   # Discover
    ), '{{CC}}', 'Credit Card'),

    # US phone numbers: (123) 456-7890, 123-456-7890, etc.
    (re.compile(
        r'\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})'
    ), '({{PHONE}})', 'Phone'),

    # Email addresses (but not common code patterns like localhost)
    (re.compile(
        r'\b[A-Za-z0-9._%+-]+@(?!localhost\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
    ), '{{EMAIL}}', 'Email'),

    # US mailing addresses (street + city/state/zip pattern)
    # e.g., "123 Main St, Springfield, IL 62704"
    (re.compile(
        r'\b\d+\s+[A-Za-z0-9\s,.]+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Rd|Road|Ct|Court|Pl|Place|Way|#\d+)\s*,\s*[A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b'
    ), '{{ADDRESS}}', 'Address'),

    # Bank routing numbers (9 digits, not SSN format — typically preceded by routing context)
    (re.compile(
        r'\b(?:routing|aba|bank\s*routing)\s*(?:number|no\.?|#)?:?\s*\d{9}\b',
        re.IGNORECASE
    ), '{{ROUTING}}', 'Bank Routing'),

    # Bank account numbers (context-dependent, 8-17 digits with bank/routing context)
    (re.compile(
        r'\b(?:account\s*(?:number|no\.?|#)?)\s*:?\s*\d{8,17}\b',
        re.IGNORECASE
    ), '{{ACCT}}', 'Bank Account'),

    # Dates of birth in common formats (DOB context)
    (re.compile(
        r'\b(?:date\s*of\s*birth|DOB|born(?:\s+on)?)\s*:?\s*(?:\d{1,2}[/\-.]\s*){2}\d{2,4}\b',
        re.IGNORECASE
    ), '{{DOB}}', 'Date of Birth'),

    # Passport numbers (US: 9 digits, often starting with letters internationally)
    (re.compile(
        r'\b(?:passport\s*(?:number|no\.?|#)?)\s*:?\s*[A-Z]?\d{8,9}\b',
        re.IGNORECASE
    ), '{{PASSPORT}}', 'Passport'),

    # Driver's license numbers (varies by state, but common pattern: 1 letter + 8-12 digits)
    (re.compile(
        r"""\b(?:driver(?:'s|\\')?\s*(?:license|lic(?:ense)?)\s*(?:number|no\.?#)?)\s*:?\s*[A-Z]?\d{7,13}\b""",
        re.IGNORECASE
    ), '{{DL}}', 'Driver License'),

    # Salary / compensation with dollar amounts (contextual)
    (re.compile(
        r'\b(?:salary|annual\s*salary|base\s*pay|compensation|hourly\s*rate|wage|pay\s*rate)\s*:?\s*\$?[\d,]+(?:\.\d{2})?(?:\s*(?:per\s*)?(?:year|annum|month|hour|hr))?\b',
        re.IGNORECASE
    ), '{{SALARY}}', 'Salary'),

    # IP addresses (internal infrastructure)
    (re.compile(
        r'\b(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\b'
    ), '{{IP}}', 'IP Address'),

    # AWS access key IDs
    (re.compile(
        r'\bAKIA[0-9A-Z]{16}\b'
    ), '{{AWS_KEY}}', 'AWS Key'),

    # Database connection strings with passwords
    (re.compile(
        r'\b(?:mysql|postgres|postgresql|mongodb|redis)://[^\s:]+:[^\s@]+@[^\s]+',
        re.IGNORECASE
    ), '{{DB_CONN}}', 'Database Connection'),
]


def detect_pii(text: str) -> list[dict]:
    """Scan text and return a list of detected PII matches.

    Returns:
        List of dicts with keys: pattern, label, match, start, end
    """
    findings = []
    for regex, placeholder, label in PATTERNS:
        for m in regex.finditer(text):
            findings.append({
                'label': label,
                'match': m.group(),
                'start': m.start(),
                'end': m.end(),
                'placeholder': placeholder,
            })
    # Sort by position
    findings.sort(key=lambda f: f['start'])
    return findings


def sanitize_for_llm(text: str, enabled: bool = True) -> tuple[str, list[dict]]:
    """Sanitize text by redacting detected PII patterns.

    Args:
        text: The text to sanitize (prompt, file content, tool output, etc.)
        enabled: If False, return text unchanged (sanitization disabled)

    Returns:
        Tuple of (sanitized_text, redaction_log).
        redaction_log is a list of dicts describing what was redacted.
    """
    if not enabled or not text:
        return text, []

    findings = detect_pii(text)
    if not findings:
        return text, []

    # Build redacted text by replacing matches in reverse order (preserve offsets)
    redacted = text
    log = []
    # Deduplicate overlapping matches by tracking replaced ranges
    replaced_ranges = []

    for finding in reversed(findings):
        start, end = finding['start'], finding['end']
        # Skip if this range overlaps with an already-replaced range
        if any(start < r[1] and end > r[0] for r in replaced_ranges):
            continue
        redacted = redacted[:start] + finding['placeholder'] + redacted[end:]
        replaced_ranges.append((start, end))
        log.append({
            'label': finding['label'],
            'placeholder': finding['placeholder'],
            'original': finding['match'][:50],  # Truncate long matches in log
        })

    log.reverse()  # Return in text order

    if log:
        summary = ', '.join(f"{l['label']}({l['placeholder']})" for l in log)
        logger.info("PII redacted: %s", summary)

    return redacted, log


def sanitize_messages(messages: list[dict], enabled: bool = True) -> tuple[list[dict], list[dict]]:
    """Sanitize all messages in a conversation before sending to LLM.

    Handles both string content and list-of-content-blocks format (for multimodal).

    Args:
        messages: List of message dicts with 'role' and 'content'.
        enabled: If False, return messages unchanged.

    Returns:
        Tuple of (sanitized_messages, combined_redaction_log).
    """
    if not enabled or not messages:
        return messages, []

    all_redactions = []
    sanitized = []

    for msg in messages:
        content = msg.get('content', '')
        role = msg.get('role', '')

        if isinstance(content, str):
            clean, redactions = sanitize_for_llm(content, enabled=True)
            if redactions:
                all_redactions.extend(redactions)
            sanitized_msg = dict(msg)
            sanitized_msg['content'] = clean
            sanitized.append(sanitized_msg)
        elif isinstance(content, list):
            # Multimodal content blocks — only sanitize text blocks
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

/**
 * SaaSClaw PII Guard — Pi extension for content-level PII sanitization.
 *
 * Intercepts the `context` event (fires before every LLM call) and
 * redacts sensitive patterns (SSNs, credit cards, phone numbers, emails,
 * addresses, salaries, bank details, DB connection strings, API keys).
 *
 * Also intercepts `tool_result` to sanitize tool outputs as they return.
 *
 * Placement: ~/.pi/agent/extensions/pii-guard.ts (global auto-discovery)
 *
 * Redactions are logged to stderr for audit trail collection.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { writeFileSync, appendFileSync, mkdirSync, existsSync } from "node:fs";
import { join } from "node:path";

// ── PII Patterns ──────────────────────────────────────────────────────────

interface Pattern {
  regex: RegExp;
  placeholder: string;
  label: string;
}

const PATTERNS: Pattern[] = [
  // US Social Security Numbers: 123-45-6789, 123 45 6789
  { regex: /\b(?!000|666|9\d{2})\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b/g, placeholder: "{{SSN}}", label: "SSN" },

  // Credit card numbers (Visa, Mastercard, Amex, Discover)
  { regex: /\b(?:4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}|5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}|3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}|6(?:011|5\d{2})[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4})\b/g, placeholder: "{{CC}}", label: "Credit Card" },

  // US phone numbers
  { regex: /\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}/g, placeholder: "{{PHONE}}", label: "Phone" },

  // Email addresses (excluding localhost)
  { regex: /\b[A-Za-z0-9._%+-]+@(?!localhost\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, placeholder: "{{EMAIL}}", label: "Email" },

  // US mailing addresses (street + city/state/zip)
  { regex: /\b\d+\s+[A-Za-z0-9\s,.]+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Rd|Road|Ct|Court|Pl|Place|Way|#\d+)\s*,\s*[A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b/g, placeholder: "{{ADDRESS}}", label: "Address" },

  // Bank routing numbers (context keyword)
  { regex: /\b(?:bank[_\s-]*routing|routing[_\s-]*(?:number|no\.?|#)?)\s*:?\s*\d{9}\b/gi, placeholder: "{{ROUTING}}", label: "Bank Routing" },

  // Bank account numbers (context keyword)
  { regex: /\b(?:bank[_\s-]*account|account[_\s-]*(?:number|no\.?|#)?)\s*:?\s*\d{8,17}\b/gi, placeholder: "{{ACCT}}", label: "Bank Account" },

  // Dates of birth (context keyword — flexible for JSON, forms, text)
  { regex: /date[_\s-]*of[_\s-]*birth[\s":,]*\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}/gi, placeholder: "{{DOB}}", label: "Date of Birth" },
  { regex: /\bdob[\s":,]*\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}\b/gi, placeholder: "{{DOB}}", label: "Date of Birth" },

  // Passport numbers (context keyword)
  { regex: /\b(?:passport[_\s-]?(?:number|no\.?|#|id)?)\s*:?\s*[A-Z]?\d{8,9}\b/gi, placeholder: "{{PASSPORT}}", label: "Passport" },

  // Salary/compensation (context keyword — broadened)
  { regex: /\b(?:salary|annual[_\s-]*salary|base[_\s-]*pay|compensation|hourly[_\s-]*rate|wage|pay[_\s-]*rate|pay[_\s-]*details|earnings|income)\s*:?\s*\$?[\d,]+(?:\.\d{2})?(?:\s*(?:per\s*)?(?:year|annum|month|hour|hr))?\b/gi, placeholder: "{{SALARY}}", label: "Salary" },

  // AWS access keys
  { regex: /\bAKIA[0-9A-Z]{16}\b/g, placeholder: "{{AWS_KEY}}", label: "AWS Key" },

  // Database connection strings with embedded credentials
  { regex: /\b(?:mysql|postgres|postgresql|mongodb|redis):\/\/[^\s:]+:[^\s@]+@[^\s]+\b/gi, placeholder: "{{DB_CONN}}", label: "DB Connection" },

  // IP addresses
  { regex: /\b(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\b/g, placeholder: "{{IP}}", label: "IP Address" },
];

// ── Audit Log ────────────────────────────────────────────────────────────

const LOG_DIR = "/var/log/saasclaw";
const LOG_FILE = join(LOG_DIR, "pii-guard.log");

function auditLog(message: string, details?: Record<string, unknown>) {
  const entry = JSON.stringify({
    ts: new Date().toISOString(),
    msg: message,
    ...details,
  });
  // Try to write to the log file; fall back to stderr
  if (existsSync(LOG_DIR)) {
    try { appendFileSync(LOG_FILE, entry + "\n"); return; } catch {}
  }
  process.stderr.write(`[pii-guard] ${entry}\n`);
}

// ── Sanitization ─────────────────────────────────────────────────────────

interface Redaction {
  label: string;
  placeholder: string;
  original: string;
}

function sanitizeText(text: string): { clean: string; redactions: Redaction[] } {
  const redactions: Redaction[] = [];
  let clean = text;

  for (const pattern of PATTERNS) {
    pattern.regex.lastIndex = 0;
    let match: RegExpExecArray | null;
    const originals: string[] = [];
    while ((match = pattern.regex.exec(clean)) !== null) {
      originals.push(match[0]);
    }
    if (originals.length > 0) {
      pattern.regex.lastIndex = 0;
      clean = clean.replace(pattern.regex, pattern.placeholder);
      for (const orig of originals) {
        redactions.push({
          label: pattern.label,
          placeholder: pattern.placeholder,
          original: orig.substring(0, 50),
        });
      }
    }
  }

  return { clean, redactions };
}

// ── Extension ────────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  pi.on("session_start", (_event, ctx) => {
    auditLog("pii-guard loaded", { cwd: ctx.cwd });
  });

  // Sanitize messages before each LLM call (context event)
  pi.on("context", async (event, ctx) => {
    const messages = event.messages;
    let totalRedactions = 0;
    const allRedactions: Redaction[] = [];

    for (const message of messages) {
      if (typeof message.content === "string") {
        const { clean, redactions } = sanitizeText(message.content);
        if (redactions.length > 0) {
          message.content = clean;
          totalRedactions += redactions.length;
          allRedactions.push(...redactions);
        }
      } else if (Array.isArray(message.content)) {
        for (const block of message.content) {
          if (block.type === "text" && typeof block.text === "string") {
            const { clean, redactions } = sanitizeText(block.text);
            if (redactions.length > 0) {
              block.text = clean;
              totalRedactions += redactions.length;
              allRedactions.push(...redactions);
            }
          }
        }
      }
    }

    if (totalRedactions > 0) {
      const summary = allRedactions.map(r => `${r.label}(${r.placeholder})`).join(", ");
      auditLog("redacted context", { count: totalRedactions, patterns: summary, cwd: ctx.cwd });
      ctx.ui.notify(`🔒 PII Guard: ${totalRedactions} pattern(s) redacted before LLM call`, "info");
    }

    return { messages };
  });

  // Sanitize tool results as they come back
  pi.on("tool_result", async (event, ctx) => {
    if (event.content && Array.isArray(event.content)) {
      let modified = false;
      for (const block of event.content) {
        if (block.type === "text" && typeof block.text === "string") {
          const { clean, redactions } = sanitizeText(block.text);
          if (redactions.length > 0) {
            block.text = clean;
            modified = true;
            const summary = redactions.map(r => `${r.label}(${r.placeholder})`).join(", ");
            auditLog("redacted tool_result", { tool: event.toolName, count: redactions.length, patterns: summary, cwd: ctx.cwd });
            ctx.ui.notify(`🔒 PII Guard: ${redactions.length} pattern(s) redacted from ${event.toolName}`, "info");
          }
        }
      }
      if (modified) return { content: event.content };
    }
  });
}

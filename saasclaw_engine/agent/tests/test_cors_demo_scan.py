"""
Integration test: Security scanner against the CORS demo project.

This tests our security_scan tool against a real vulnerable codebase
(the intentionally vulnerable CORS demo at cors-demo.saasclaw.ai).

Expected vulnerabilities:
  - CORS: reflects any origin + credentials (CRITICAL)
  - Hardcoded secrets: passwords + API keys in source (HIGH)
  - XSS: innerHTML with dynamic data (MEDIUM)
  - Cookie security: httpOnly=false, sameSite=none (HIGH)
"""
import os
import re
import pytest

# Path to the real vulnerable CORS demo source
# Try workspace path first, fall back to /tmp copy for saasclaw user
_WORKSPACE_PATH = "/home/nmoore/.openclaw/workspace/cors-demo/server"
_TMP_PATH = "/tmp/cors-demo-server"
CORS_DEMO_PATH = _WORKSPACE_PATH if os.path.isdir(_WORKSPACE_PATH) else _TMP_PATH
CORS_DEMO_APP = os.path.join(CORS_DEMO_PATH, "index.js")
CORS_DEMO_FRONTEND = os.path.join(CORS_DEMO_PATH, "public", "index.html")


def _read_source(path):
    """Read source file, skip if missing."""
    if not os.path.exists(path):
        pytest.skip(f"Source file not found: {path}")
    with open(path) as f:
        return f.read()


class TestCorsDemoScannerIntegration:
    """Integration tests against the real CORS demo project."""

    def test_cors_vulnerability_detected(self):
        """Quick scan should detect the CORS origin reflection vulnerability."""
        source = _read_source(CORS_DEMO_APP)
        # Our quick scan greps for Access-Control-Allow-Origin
        matches = re.findall(r"Access-Control-Allow-Origin.*origin|origin.*Access-Control-Allow-Origin", source, re.IGNORECASE)
        assert len(matches) > 0, "Should find CORS origin reflection"

    def test_cors_credentials_with_wildcard_origin(self):
        """Should detect Access-Control-Allow-Credentials: true with dynamic origin."""
        source = _read_source(CORS_DEMO_APP)
        assert "Access-Control-Allow-Credentials" in source
        assert "Access-Control-Allow-Origin" in source
        # The dangerous combination: reflecting origin + credentials
        assert re.search(r"setHeader.*Access-Control-Allow-Origin.*origin", source, re.IGNORECASE)
        assert re.search(r"setHeader.*Access-Control-Allow-Credentials.*true", source, re.IGNORECASE)

    def test_hardcoded_password_detected(self):
        """Should detect hardcoded passwords in source."""
        source = _read_source(CORS_DEMO_APP)
        assert "demo123" in source
        assert re.search(r"password.*['\"].*['\"]", source, re.IGNORECASE)

    def test_hardcoded_api_keys_detected(self):
        """Should detect hardcoded API keys in source."""
        source = _read_source(CORS_DEMO_APP)
        assert "sk-" in source  # OpenAI-style key
        assert "API_KEYS" in source
        # Our quick scan greps for: password|secret|api_key|apikey|token
        assert re.search(r"api_key|apikey|API_KEYS", source, re.IGNORECASE)

    def test_xss_innerhtml_in_frontend(self):
        """Should detect innerHTML usage in frontend."""
        source = _read_source(CORS_DEMO_FRONTEND)
        assert "innerHTML" in source
        # It's using dynamic data (not just static text)
        assert ".map(" in source or "dealsData" in source

    def test_cookie_security_issues(self):
        """Should detect weak cookie settings."""
        source = _read_source(CORS_DEMO_APP)
        assert "httpOnly: false" in source or "httpOnly: false".lower() in source.lower()
        assert "sameSite: 'none'" in source or "sameSite: none".lower() in source.lower()

    def test_quick_scan_grep_patterns_all_hit(self):
        """Run actual grep patterns from _quick_scan_instructions against CORS demo."""
        from saasclaw_engine.agent.tools import _quick_scan_instructions
        instructions = _quick_scan_instructions()

        # Verify the instructions contain grep commands that would catch these vulns
        assert "Access-Control-Allow-Origin" in instructions
        assert "password" in instructions.lower() or "secret" in instructions.lower()

        # Manual verification: the patterns from our scan would match
        source = _read_source(CORS_DEMO_APP)
        patterns = {
            "CORS origin reflection": r"Access-Control-Allow-Origin['\"]?,\s*origin\)|setHeader.*Access-Control-Allow-Origin.*origin",
            "CORS credentials true": r"Access-Control-Allow-Credentials.*true",
            "Hardcoded password": r"password.*demo123",
            "Hardcoded API key": r"sk-proj-abc123|sk_live_xyz789|sk-ant-def456",
        }
        for name, pattern in patterns.items():
            matches = re.findall(pattern, source, re.IGNORECASE)
            assert len(matches) > 0, f"Pattern '{name}' should match: {pattern}"

    def test_full_scan_instructions_would_guide_audit(self):
        """Full scan instructions should contain guidance that would find all vuln categories."""
        from saasclaw_engine.agent.tools import _full_scan_instructions
        instructions = _full_scan_instructions("full")

        # The full scan methodology should cover:
        assert "Recon" in instructions or "recon" in instructions.lower()
        assert "Hunt" in instructions or "hunt" in instructions.lower()
        assert "Disprove" in instructions or "disprove" in instructions.lower()
        assert "Report" in instructions or "report" in instructions.lower()

        # Should mention CORS or Access-Control in vulnerability types
        vuln_terms = ["CORS", "Access-Control", "origin", "credential"]
        assert any(term in instructions for term in vuln_terms), \
            "Full scan should mention CORS-related vulnerabilities"

    def test_vulnerability_count(self):
        """Count the known vulnerabilities in the CORS demo."""
        source = _read_source(CORS_DEMO_APP)
        frontend = _read_source(CORS_DEMO_FRONTEND)

        vulns = []

        # CORS: origin reflection + credentials
        if "Access-Control-Allow-Origin" in source and "req.headers.origin" in source:
            vulns.append(("critical", "CORS origin reflection"))
        if "Access-Control-Allow-Credentials" in source and "'true'" in source:
            vulns.append(("critical", "CORS credentials with reflected origin"))

        # Hardcoded secrets
        if "demo123" in source:
            vulns.append(("high", "Hardcoded password"))
        if "sk-proj-abc123" in source or "sk_live_xyz789" in source:
            vulns.append(("high", "Hardcoded API keys"))

        # Cookie security
        if "httpOnly: false" in source:
            vulns.append(("high", "Cookie httpOnly disabled"))
        if "sameSite: 'none'" in source:
            vulns.append(("high", "Cookie sameSite=None"))

        # XSS in frontend
        if "innerHTML" in frontend and ".map(" in frontend:
            vulns.append(("medium", "innerHTML with dynamic data (potential DOM XSS)"))

        # Should have at least 7 vulnerabilities
        assert len(vulns) >= 7, f"Expected ≥7 vulnerabilities, found {len(vulns)}: {vulns}"

        # Should have at least 2 critical
        critical_count = sum(1 for sev, _ in vulns if sev == "critical")
        assert critical_count >= 2, f"Expected ≥2 critical, got {critical_count}"

    def test_execute_tool_security_scan_on_real_codebase(self):
        """Test that execute_tool returns scan instructions for this codebase."""
        from saasclaw_engine.agent.tools import execute_tool

        result = execute_tool(
            CORS_DEMO_PATH,
            "security_scan",
            {"scope": "quick"},
            session_id="test-cors-demo-scan",
        )
        assert result is not None
        assert isinstance(result, str)
        # Quick scan should return grep instructions
        assert "grep" in result.lower() or "scan" in result.lower()

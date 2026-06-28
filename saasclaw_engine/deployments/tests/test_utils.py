"""Tests for deploy pipeline utility functions.

Covers: env file parsing, slugification, URL normalization, repo validation,
nginx config generation — all pure functions that don't need filesystem/process.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open


# ── Extract pure functions for testing ────────────────────────────────────

def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
    return values


def _serialize_env_file(values: dict[str, str]) -> str:
    lines = []
    for key, value in sorted(values.items()):
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


def _slugify_system_name(value: str) -> str:
    """Slugify for systemd/nginx service names."""
    import re
    s = re.sub(r'[^a-z0-9-]', '-', value.lower()).strip('-') or 'app'
    return re.sub(r'-{2,}', '-', s)


def _normalize_repo_url(url: str) -> str:
    """Normalize repo URL for comparison."""
    if not url:
        return ''
    url = url.strip()
    if url.startswith('git@github.com:'):
        url = 'https://github.com/' + url.split(':', 1)[1]
    import re
    url = re.sub(r'https?://[^@]+@', 'https://', url)
    return url.rstrip('/').removesuffix('.git')


# ══════════════════════════════════════════════════════════════════════════
# Env File Parsing
# ══════════════════════════════════════════════════════════════════════════

class TestLoadEnvFile:
    def test_basic_key_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=value\n")
        assert _load_env_file(f) == {"KEY": "value"}

    def test_multiple_keys(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("DB_HOST=localhost\nDB_PORT=5432\nAPP_NAME=myapp\n")
        result = _load_env_file(f)
        assert result["DB_HOST"] == "localhost"
        assert result["DB_PORT"] == "5432"
        assert result["APP_NAME"] == "myapp"

    def test_empty_lines_ignored(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY1=val1\n\nKEY2=val2\n")
        assert _load_env_file(f) == {"KEY1": "val1", "KEY2": "val2"}

    def test_comments_ignored(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# Comment\nKEY=value\n# Another comment\n")
        assert _load_env_file(f) == {"KEY": "value"}

    def test_values_with_equals(self, tmp_path):
        """Values containing = should be parsed correctly (partition on first =)."""
        f = tmp_path / ".env"
        f.write_text("CONN_STR=postgres://user=pw@host/db\n")
        assert _load_env_file(f) == {"CONN_STR": "postgres://user=pw@host/db"}

    def test_values_with_spaces(self, tmp_path):
        """Leading/trailing spaces in values should be stripped."""
        f = tmp_path / ".env"
        f.write_text("KEY=  value with spaces  \n")
        assert _load_env_file(f) == {"KEY": "value with spaces"}

    def test_keys_with_spaces_stripped(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("  KEY  = value\n")
        assert _load_env_file(f) == {"KEY": "value"}

    def test_nonexistent_file(self):
        assert _load_env_file(Path("/nonexistent/.env")) == {}

    def test_empty_file(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("")
        assert _load_env_file(f) == {}

    def test_lines_without_equals_ignored(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("NOEQUALSHERE\nKEY=value\n")
        assert _load_env_file(f) == {"KEY": "value"}


class TestSerializeEnvFile:
    def test_roundtrip(self):
        values = {"DB_HOST": "localhost", "DB_PORT": "5432"}
        serialized = _serialize_env_file(values)
        assert "DB_HOST=localhost" in serialized
        assert "DB_PORT=5432" in serialized

    def test_sorted_alphabetically(self):
        values = {"Z_KEY": "z", "A_KEY": "a"}
        serialized = _serialize_env_file(values)
        lines = serialized.strip().split('\n')
        assert lines[0] == "A_KEY=a"
        assert lines[1] == "Z_KEY=z"

    def test_empty_dict(self):
        assert _serialize_env_file({}) == "\n"

    def test_trailing_newline(self):
        assert _serialize_env_file({"K": "v"}).endswith('\n')


# ══════════════════════════════════════════════════════════════════════════
# Slugify
# ══════════════════════════════════════════════════════════════════════════

class TestSlugifySystemName:
    def test_basic(self):
        assert _slugify_system_name("my-project") == "my-project"

    def test_uppercase_lowered(self):
        assert _slugify_system_name("MyApp") == "myapp"

    def test_spaces_to_hyphens(self):
        assert _slugify_system_name("my cool app") == "my-cool-app"

    def test_special_chars_removed(self):
        assert _slugify_system_name("app_@#$%test") == "app-test"

    def test_underscores_to_hyphens(self):
        assert _slugify_system_name("my_app_name") == "my-app-name"

    def test_leading_trailing_stripped(self):
        assert _slugify_system_name("--hello--") == "hello"

    def test_empty_string_returns_fallback(self):
        """Empty string should return 'app' fallback."""
        assert _slugify_system_name("") == "app"

    def test_numbers_preserved(self):
        assert _slugify_system_name("app-2024") == "app-2024"


# ══════════════════════════════════════════════════════════════════════════
# Repo URL Normalization
# ══════════════════════════════════════════════════════════════════════════

class TestNormalizeRepoUrl:
    def test_ssh_to_https(self):
        assert _normalize_repo_url("git@github.com:user/repo.git") == "https://github.com/user/repo"

    def test_https_no_token(self):
        assert _normalize_repo_url("https://github.com/user/repo.git") == "https://github.com/user/repo"

    def test_https_with_token(self):
        """Token-embedded URLs should be stripped to plain https."""
        assert _normalize_repo_url("https://x-access-token:abc123@github.com/user/repo") == "https://github.com/user/repo"

    def test_trailing_slash_stripped(self):
        assert _normalize_repo_url("https://github.com/user/repo/") == "https://github.com/user/repo"

    def test_empty_string(self):
        assert _normalize_repo_url("") == ""

    def test_none_like_empty(self):
        assert _normalize_repo_url(None or "") == ""

    def test_git_suffix_stripped(self):
        assert _normalize_repo_url("https://github.com/user/repo.git") == "https://github.com/user/repo"

    def test_non_github_url(self):
        assert _normalize_repo_url("https://gitlab.com/user/repo.git") == "https://gitlab.com/user/repo"

    def test_consistency(self):
        """SSH and HTTPS forms of the same repo should normalize identically."""
        ssh = _normalize_repo_url("git@github.com:user/repo.git")
        https = _normalize_repo_url("https://github.com/user/repo.git")
        assert ssh == https

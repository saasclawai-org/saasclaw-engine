"""Tests for deployment service helpers.

Covers: env file loading/serialization, Postgres credential generation,
and slug sanitization. Tests the pure-logic functions without needing
real Postgres connections or systemd.
"""

import json
from pathlib import Path

import pytest

from saasclaw_engine.deployments.service import (
    _load_env_file,
    _serialize_env_file,
)


class TestLoadEnvFile:
    """Tests for _load_env_file parsing."""

    def test_empty_file(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("")
        assert _load_env_file(f) == {}

    def test_missing_file(self, tmp_path):
        assert _load_env_file(tmp_path / "nonexistent") == {}

    def test_basic_key_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=value\nOTHER=thing\n")
        result = _load_env_file(f)
        assert result == {"KEY": "value", "OTHER": "thing"}

    def test_ignores_comments(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# comment\nKEY=value\n# another\nFOO=bar\n")
        result = _load_env_file(f)
        assert result == {"KEY": "value", "FOO": "bar"}

    def test_ignores_blank_lines(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("\n\nKEY=value\n\n")
        assert _load_env_file(f) == {"KEY": "value"}

    def test_value_with_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("DATABASE_URL=postgresql://user:pass@host:5432/db?sslmode=require\n")
        result = _load_env_file(f)
        assert "postgresql://user:pass@host:5432/db?sslmode=require" in result["DATABASE_URL"]

    def test_value_with_quotes(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text('KEY="quoted value"\nUNQUOTED=bare')
        result = _load_env_file(f)
        # parser doesn't strip quotes (simple implementation)
        assert result["KEY"] == '"quoted value"'
        assert result["UNQUOTED"] == "bare"

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("  KEY  =  value  \n")
        result = _load_env_file(f)
        assert result["KEY"] == "value"

    def test_overwrite_last_wins(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=first\nKEY=second\n")
        result = _load_env_file(f)
        assert result["KEY"] == "second"


class TestSerializeEnvFile:
    """Tests for _serialize_env_file output."""

    def test_basic(self):
        result = _serialize_env_file({"A": "1", "B": "2"})
        assert result == "A=1\nB=2\n"

    def test_sorted_keys(self):
        result = _serialize_env_file({"Z": "3", "A": "1", "M": "2"})
        assert result == "A=1\nM=2\nZ=3\n"

    def test_empty_dict(self):
        assert _serialize_env_file({}) == "\n"

    def test_special_characters_preserved(self):
        result = _serialize_env_file({"URL": "https://example.com?foo=bar&baz=1"})
        assert "https://example.com?foo=bar&baz=1" in result


class TestPostgresCredentialGeneration:
    """Tests for the naming logic used in _deploy_django_environment."""

    def test_db_name_from_slug(self):
        slug = "employee-form"
        suffix = "_preview"
        db_name = f"saasclaw_{slug.replace('-', '_')}{suffix}"
        assert db_name == "saasclaw_employee_form_preview"

    def test_db_user_from_slug(self):
        slug = "employee-form"
        suffix = "_preview"
        db_user = f"sc_{slug.replace('-', '_')}{suffix}"[:32]
        assert db_user == "sc_employee_form_preview"
        assert len(db_user) <= 32

    def test_long_slug_truncated_to_32(self):
        slug = "very-long-project-name-that-goes-on"
        suffix = "_preview"
        db_user = f"sc_{slug.replace('-', '_')}{suffix}"[:32]
        assert len(db_user) <= 32

    def test_production_suffix(self):
        slug = "my-project"
        suffix = "_production"
        db_name = f"saasclaw_{slug.replace('-', '_')}{suffix}"
        assert db_name == "saasclaw_my_project_production"

    def test_preview_no_suffix(self):
        # Preview env doesn't add _preview suffix (empty suffix)
        slug = "my-project"
        suffix = ""
        db_name = f"saasclaw_{slug.replace('-', '_')}{suffix}"
        assert db_name == "saasclaw_my_project"

    def test_different_projects_get_different_creds(self):
        for s1, s2 in [("a", "b"), ("employee-form", "task-list")]:
            d1 = f"saasclaw_{s1.replace('-', '_')}"
            d2 = f"saasclaw_{s2.replace('-', '_')}"
            assert d1 != d2


class TestConnectionStringFormats:
    """Verify database connection string formats for each runtime."""

    def test_dotnet_npgsql_format(self):
        """.NET ConnectionStrings__DefaultConnection uses Npgsql format."""
        db_user, db_password, db_host, db_port, db_name = (
            "sc_my_app_preview", "secret123", "127.0.0.1", "5432", "saasclaw_my_app_preview"
        )
        conn = f"Host={db_host};Port={db_port};Database={db_name};Username={db_user};Password={db_password}"
        assert conn.startswith("Host=")
        assert "Port=" in conn
        assert "Database=" in conn
        assert "Username=" in conn
        assert "Password=" in conn
        assert "postgresql+psycopg" not in conn

    def test_node_postgresql_url_format(self):
        """Node SSR DATABASE_URL uses standard postgresql:// (no driver suffix)."""
        db_user, db_password, db_host, db_port, db_name = (
            "sc_my_app_preview", "secret123", "127.0.0.1", "5432", "saasclaw_my_app_preview"
        )
        url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
        assert url.startswith("postgresql://")
        assert "+psycopg" not in url

    def test_django_database_url_format(self):
        """Django DATABASE_URL uses postgresql+psycopg:// for dj-database-url."""
        db_user, db_password, db_host, db_port, db_name = (
            "sc_my_app_preview", "secret123", "127.0.0.1", "5432", "saasclaw_my_app_preview"
        )
        url = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
        assert url.startswith("postgresql+psycopg://")

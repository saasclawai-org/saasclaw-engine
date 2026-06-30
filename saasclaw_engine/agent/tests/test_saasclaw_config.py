import json
import os
import pytest
from unittest.mock import patch

from saasclaw_engine.agent.tools import _load_saasclaw_config, _match_glob


class TestLoadSaasclawConfig:
    """Tests for _load_saasclaw_config loading .saasclaw file and .saasclaw/config.json."""

    def test_missing_config_returns_empty(self, tmp_path):
        result = _load_saasclaw_config(str(tmp_path))
        assert result == {}

    def test_file_based_config(self, tmp_path):
        config = {"version": 1, "file_limits": {"default": 300}}
        config_path = tmp_path / ".saasclaw"
        config_path.write_text(json.dumps(config))
        result = _load_saasclaw_config(str(tmp_path))
        assert result["version"] == 1
        assert result["file_limits"]["default"] == 300

    def test_directory_based_config(self, tmp_path):
        config = {"version": 1, "file_limits": {"default": 500}}
        dot_dir = tmp_path / ".saasclaw"
        dot_dir.mkdir()
        (dot_dir / "config.json").write_text(json.dumps(config))
        result = _load_saasclaw_config(str(tmp_path))
        assert result["version"] == 1
        assert result["file_limits"]["default"] == 500

    def test_directory_without_config_json_returns_empty(self, tmp_path):
        dot_dir = tmp_path / ".saasclaw"
        dot_dir.mkdir()
        (dot_dir / "context.md").write_text("# Project context")
        result = _load_saasclaw_config(str(tmp_path))
        assert result == {}

    def test_directory_with_empty_context_dir(self, tmp_path):
        dot_dir = tmp_path / ".saasclaw"
        dot_dir.mkdir()
        result = _load_saasclaw_config(str(tmp_path))
        assert result == {}

    def test_invalid_json_returns_empty(self, tmp_path):
        config_path = tmp_path / ".saasclaw"
        config_path.write_text("not json at all {{{")
        result = _load_saasclaw_config(str(tmp_path))
        assert result == {}

    def test_file_based_takes_priority_over_directory(self, tmp_path):
        """If .saasclaw is a file (not dir), it should be read directly."""
        file_config = {"version": 1, "source": "file"}
        (tmp_path / ".saasclaw").write_text(json.dumps(file_config))
        result = _load_saasclaw_config(str(tmp_path))
        assert result["source"] == "file"

    def test_config_preserves_all_fields(self, tmp_path):
        config = {
            "version": 1,
            "file_limits": {"default": 300, "src/app/page.tsx": 150},
            "architecture": {
                "rules": ["Keep components small"],
                "notes": "Custom config",
            },
        }
        (tmp_path / ".saasclaw").write_text(json.dumps(config))
        result = _load_saasclaw_config(str(tmp_path))
        assert result["file_limits"]["src/app/page.tsx"] == 150
        assert result["architecture"]["rules"] == ["Keep components small"]
        assert result["architecture"]["notes"] == "Custom config"


class TestMatchGlob:
    """Tests for _match_glob pattern matching."""

    def test_exact_match(self):
        assert _match_glob("views.py", "views.py") is True

    def test_wildcard_match(self):
        assert _match_glob("*.py", "views.py") is True
        assert _match_glob("*.py", "settings.py") is True
        assert _match_glob("*.tsx", "views.py") is False

    def test_doublestar_match(self):
        assert _match_glob("src/**/*.tsx", "src/app/page.tsx") is True
        assert _match_glob("src/**/*.tsx", "src/components/ui/Button.tsx") is True

    def test_doublestar_no_match(self):
        assert _match_glob("src/**/*.tsx", "src/app/page.ts") is False
        assert _match_glob("src/**/*.tsx", "app/page.tsx") is False

    def test_doublestar_nested(self):
        assert _match_glob("src/lib/**/*.ts", "src/lib/utils/format.ts") is True

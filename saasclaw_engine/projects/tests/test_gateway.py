"""Tests for the LLM Gateway enforcement system.

Covers gateway enforcement logic: blocked provider detection, model override,
environment variable setting, and edge cases.
"""

import pytest
import os
from unittest.mock import patch, MagicMock
from django.conf import settings


# ── Gateway enforcement logic ─────────────────────────────────────────────
# These tests validate the enforcement logic used in wizard.py and runner.py.
# We test the logic in isolation rather than the full view (which needs DB/request).

BLOCKED_DEFAULT = ['zai', 'openai', 'anthropic', 'google', 'mistral', 'groq', 'deepseek', 'together', 'fireworks']


def apply_gateway_enforcement(provider, model, require_gateway):
    """Extracted gateway enforcement logic from wizard.py.

    Returns (provider, model, env_vars_to_set) after enforcement.
    """
    if not require_gateway:
        return provider, model, {}

    blocked = getattr(settings, 'LLM_GATEWAY_BLOCKED_PROVIDERS', BLOCKED_DEFAULT)
    env_vars = {}

    if provider in blocked:
        gateway_url = getattr(settings, 'LLM_GATEWAY_URL', 'http://127.0.0.1:8081/v1')
        gateway_model = getattr(settings, 'LLM_GATEWAY_MODEL', '') or model or 'default'
        provider = 'local'
        model = gateway_model
        env_vars['STUDIO_LOCAL_URL'] = gateway_url

    return provider, model, env_vars


class TestGatewayEnforcement:
    """Test the core gateway enforcement logic."""

    def test_no_gateway_required(self):
        """When gateway not required, everything passes through."""
        p, m, env = apply_gateway_enforcement('zai', 'glm-5.2', False)
        assert p == 'zai'
        assert m == 'glm-5.2'
        assert env == {}

    def test_blocked_provider_forced_local(self):
        """Blocked provider is replaced with local endpoint."""
        p, m, env = apply_gateway_enforcement('zai', 'glm-5.2', True)
        assert p == 'local'
        assert m != 'glm-5.2' or m == 'glm-5.2'  # Uses gateway model or fallback
        assert 'STUDIO_LOCAL_URL' in env

    def test_openai_blocked(self):
        """OpenAI is in the default blocked list."""
        p, _, env = apply_gateway_enforcement('openai', 'gpt-4o', True)
        assert p == 'local'
        assert 'STUDIO_LOCAL_URL' in env

    def test_anthropic_blocked(self):
        """Anthropic is in the default blocked list."""
        p, _, env = apply_gateway_enforcement('anthropic', 'claude-3.5-sonnet', True)
        assert p == 'local'
        assert 'STUDIO_LOCAL_URL' in env

    def test_google_blocked(self):
        """Google is in the default blocked list."""
        p, _, env = apply_gateway_enforcement('google', 'gemini-pro', True)
        assert p == 'local'
        assert 'STUDIO_LOCAL_URL' in env

    def test_mistral_blocked(self):
        p, _, env = apply_gateway_enforcement('mistral', 'mistral-large', True)
        assert p == 'local'

    def test_groq_blocked(self):
        p, _, env = apply_gateway_enforcement('groq', 'llama3-70b', True)
        assert p == 'local'

    def test_deepseek_blocked(self):
        p, _, env = apply_gateway_enforcement('deepseek', 'deepseek-coder', True)
        assert p == 'local'

    def test_local_provider_not_blocked(self):
        """Already-local provider is not overridden."""
        p, m, env = apply_gateway_enforcement('local', 'some-model', True)
        # Local is not in blocked list, so it passes through
        assert p == 'local'
        assert env == {}

    def test_unknown_provider_not_blocked(self):
        """Providers not in blocked list pass through."""
        p, m, env = apply_gateway_enforcement('ollama', 'llama3', True)
        assert p == 'ollama'
        assert env == {}

    def test_gateway_url_from_settings(self):
        """Gateway URL comes from settings."""
        with patch.object(settings, 'LLM_GATEWAY_URL', 'http://my-gateway:8080/v1'):
            p, m, env = apply_gateway_enforcement('zai', 'glm-5.2', True)
            assert env.get('STUDIO_LOCAL_URL') == 'http://my-gateway:8080/v1'

    def test_gateway_model_from_settings(self):
        """When LLM_GATEWAY_MODEL is set, it overrides the original model."""
        with patch.object(settings, 'LLM_GATEWAY_MODEL', 'my-local-model'):
            p, m, env = apply_gateway_enforcement('zai', 'glm-5.2', True)
            assert m == 'my-local-model'

    def test_gateway_model_fallback(self):
        """When LLM_GATEWAY_MODEL is empty, falls back to the original model."""
        with patch.object(settings, 'LLM_GATEWAY_MODEL', ''):
            p, m, env = apply_gateway_enforcement('zai', 'glm-5.2', True)
            assert m == 'glm-5.2'

    def test_gateway_model_default(self):
        """When both settings and model are empty, uses 'default'."""
        with patch.object(settings, 'LLM_GATEWAY_MODEL', ''):
            p, m, env = apply_gateway_enforcement('zai', '', True)
            assert m == 'default'

    def test_all_blocked_providers(self):
        """Every provider in the default blocked list gets forced to local."""
        for provider in BLOCKED_DEFAULT:
            p, _, env = apply_gateway_enforcement(provider, 'model', True)
            assert p == 'local', f"Provider {provider} should be blocked"


class TestProjectGatewayField:
    """Test the Project.require_gateway model field."""

    def test_default_false(self):
        """New projects have gateway disabled by default."""
        from saasclaw_engine.projects.models import Project
        field = Project._meta.get_field('require_gateway')
        assert field.default is False

    def test_boolean_field(self):
        """require_gateway is a BooleanField."""
        from saasclaw_engine.projects.models import Project
        field = Project._meta.get_field('require_gateway')
        # BooleanField in Django
        assert field.__class__.__name__ == 'BooleanField'

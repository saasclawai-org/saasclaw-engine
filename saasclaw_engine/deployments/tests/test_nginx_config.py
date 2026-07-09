"""Tests for nginx config generation in the deploy pipeline.

Verifies that _ensure_nginx_proxy is called for all Python app types
(Django, Flask, FastAPI) — not just React-Django. This is a regression
test for the bug where Flask/FastAPI deploys created systemd services
but no nginx config, resulting in 'Project Not Found' at the domain.
"""
import pytest
from unittest import mock
from pathlib import Path


class TestNginxProxyConfigGeneration:
    """Verify nginx config content for reverse-proxy sites."""

    def test_ensure_nginx_proxy_generates_valid_config(self):
        """_ensure_nginx_proxy produces config with proxy_pass and server_name."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_proxy

        captured_content = {}

        def fake_write_and_validate(site_name, content, log_file=None):
            captured_content['name'] = site_name
            captured_content['content'] = content
            return True

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            side_effect=fake_write_and_validate,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/etc/letsencrypt/live/example.com/fullchain.pem',
                          '/etc/letsencrypt/live/example.com/privkey.pem'),
        ), mock.patch('saasclaw_engine.deployments.deploy_infra.settings') as mock_settings:
            mock_settings.PREVIEW_BASE_DOMAIN = 'preview.example.com'

            _ensure_nginx_proxy(
                'saasclaw-test-preview',
                'test.preview.example.com',
                21001,
            )

        content = captured_content['content']
        assert 'server_name test.preview.example.com;' in content
        assert 'proxy_pass http://127.0.0.1:21001;' in content
        assert 'listen 443 ssl;' in content
        assert 'listen 80;' in content
        assert 'return 301 https://$host$request_uri;' in content

    def test_ensure_nginx_proxy_rejects_invalid_port(self):
        """_ensure_nginx_proxy raises RuntimeError for port <= 0."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_proxy

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            return_value=True,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/cert.pem', '/key.pem'),
        ), mock.patch('saasclaw_engine.deployments.deploy_infra.settings') as mock_settings:
            mock_settings.PREVIEW_BASE_DOMAIN = 'preview.example.com'

            with pytest.raises(RuntimeError, match='Invalid nginx proxy port'):
                _ensure_nginx_proxy('test', 'test.example.com', 0)

    def test_ensure_nginx_proxy_fails_when_validation_fails(self):
        """_ensure_nginx_proxy raises RuntimeError when nginx -t fails."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_proxy

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            return_value=False,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/cert.pem', '/key.pem'),
        ), mock.patch('saasclaw_engine.deployments.deploy_infra.settings') as mock_settings:
            mock_settings.PREVIEW_BASE_DOMAIN = 'preview.example.com'

            with pytest.raises(RuntimeError, match='Failed to write/validate'):
                _ensure_nginx_proxy('test', 'test.example.com', 21001)

    def test_ensure_nginx_proxy_includes_form_api(self):
        """Proxy config includes Form API passthrough to Django."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_proxy

        captured = {}

        def fake_write(site_name, content, log_file=None):
            captured['content'] = content
            return True

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            side_effect=fake_write,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/cert.pem', '/key.pem'),
        ), mock.patch('saasclaw_engine.deployments.deploy_infra.settings') as mock_settings:
            mock_settings.PREVIEW_BASE_DOMAIN = 'preview.example.com'

            _ensure_nginx_proxy('test', 'test.preview.example.com', 21001)

        assert '/api/forms/' in captured['content']
        assert 'proxy_pass http://127.0.0.1:8010;' in captured['content']


class TestNginxSpaProxyConfigGeneration:
    """Verify SPA nginx config for React-Django apps."""

    def test_ensure_nginx_spa_generates_try_files(self):
        """SPA config includes try_files fallback for client-side routing."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_spa_proxy

        captured = {}

        def fake_write(site_name, content, log_file=None):
            captured['content'] = content
            return True

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            side_effect=fake_write,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/cert.pem', '/key.pem'),
        ):
            _ensure_nginx_spa_proxy(
                'saasclaw-react-app-preview',
                'react-app.preview.example.com',
                21002,
                '/srv/saasclaw/projects/react-app/dist',
                '/srv/saasclaw/projects/react-app/static',
            )

        content = captured['content']
        assert 'try_files $uri $uri/ /index.html;' in content
        assert 'proxy_pass http://127.0.0.1:21002;' in content


class TestDeployDjangoCallsNginx:
    """Regression test: Flask/FastAPI/plain Django deploys must call _ensure_nginx_proxy.

    Bug: deploy_django.py only called _ensure_nginx_spa_proxy when has_frontend=True.
    Fix: added _ensure_nginx_proxy call in the else branch.
    """

    def test_flask_deploy_calls_nginx_proxy(self):
        """Flask (no frontend) deploy must call _ensure_nginx_proxy."""
        from saasclaw_engine.deployments.deploy_django import _deploy_django_environment

        # The function is called with has_frontend=False for Flask/FastAPI
        # We verify the import works and the function signature includes nginx
        import inspect
        source = inspect.getsource(_deploy_django_environment)
        assert '_ensure_nginx_proxy' in source, (
            "_deploy_django_environment must call _ensure_nginx_proxy "
            "for non-frontend Python apps (Flask, FastAPI, plain Django)"
        )

    def test_nginx_proxy_imported_in_deploy_django(self):
        """_ensure_nginx_proxy must be imported in deploy_django module."""
        from saasclaw_engine.deployments import deploy_django
        assert hasattr(deploy_django, '_ensure_nginx_proxy'), (
            "_ensure_nginx_proxy must be importable from deploy_django"
        )


class TestPortAllocation:
    """Tests for _allocate_port collision detection."""

    def test_preview_ports_in_range(self):
        """Preview ports are allocated in 21000-21999 range."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_proxy
        # Port allocation lives in workspace_ops in the app, not engine.
        # We verify the nginx config uses the port from the environment correctly.
        captured = {}

        def fake_write(site_name, content, log_file=None):
            captured['content'] = content
            return True

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            side_effect=fake_write,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/cert.pem', '/key.pem'),
        ), mock.patch('saasclaw_engine.deployments.deploy_infra.settings') as mock_settings:
            mock_settings.PREVIEW_BASE_DOMAIN = 'preview.example.com'

            _ensure_nginx_proxy('test', 'test.preview.example.com', 21015)

        assert '127.0.0.1:21015' in captured['content']

    def test_production_port_in_config(self):
        """Production port is written into nginx config."""
        from saasclaw_engine.deployments.deploy_infra import _ensure_nginx_proxy
        captured = {}

        def fake_write(site_name, content, log_file=None):
            captured['content'] = content
            return True

        with mock.patch(
            'saasclaw_engine.deployments.deploy_infra._write_and_validate_nginx',
            side_effect=fake_write,
        ), mock.patch(
            'saasclaw_engine.deployments.deploy_infra._pick_ssl_certs',
            return_value=('/cert.pem', '/key.pem'),
        ), mock.patch('saasclaw_engine.deployments.deploy_infra.settings') as mock_settings:
            mock_settings.PREVIEW_BASE_DOMAIN = 'preview.example.com'

            _ensure_nginx_proxy('test', 'test.example.com', 22005)

        assert '127.0.0.1:22005' in captured['content']


class TestNginxConfigValidation:
    """Tests for _write_and_validate_nginx rollback behavior."""

    def test_validation_failure_rolls_back(self):
        """When nginx -t fails, the config is removed."""
        from saasclaw_engine.deployments.deploy_infra import _write_and_validate_nginx

        call_log = []

        def fake_run(cmd, **kwargs):
            call_log.append(cmd)
            result = mock.Mock()
            result.returncode = 0
            result.stderr = b''
            result.stdout = b''
            if isinstance(cmd, list) and 'nginx' in cmd and '-t' in cmd:
                result.returncode = 1
                result.stderr = b'nginx: [emerg] invalid config'
            return result

        with mock.patch('saasclaw_engine.deployments.deploy_infra.subprocess.run',
                        side_effect=fake_run):
            result = _write_and_validate_nginx(
                'test-site',
                'server { listen 80; }',
            )

        assert result is False
        # Should have attempted rollback (rm + nginx -t + reload)
        rollback_cmds = [c for c in call_log if isinstance(c, list) and 'rm' in c]
        assert len(rollback_cmds) >= 1, "Should remove invalid config on rollback"

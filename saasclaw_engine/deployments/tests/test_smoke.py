"""Tests for the smoke test runner."""
from unittest.mock import patch, MagicMock
from saasclaw_engine.deployments.smoke_tests import smoke_test_deploy


class TestSmokeTestDeploy:

    @patch('saasclaw_engine.deployments.smoke_tests.urllib.request.urlopen')
    def test_healthy_response(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'<html><body>Hello</body></html>'
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        result = smoke_test_deploy("https://example.com", max_wait=1)
        assert result["status_code"] == 200
        assert result["error"] is None

    @patch('saasclaw_engine.deployments.smoke_tests.urllib.request.urlopen')
    def test_detects_error_markers(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'<html>500 Internal Server Error</html>'
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        result = smoke_test_deploy("https://example.com", max_wait=1)
        assert result["healthy"] is False
        assert "500 internal" in result["error"].lower()

    @patch('saasclaw_engine.deployments.smoke_tests.urllib.request.urlopen')
    def test_502_retries(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 502, "Bad Gateway", {}, MagicMock()
        )
        result = smoke_test_deploy("https://example.com", max_wait=2)
        assert result["healthy"] is False
        assert result["status_code"] == 502

    def test_no_base_url_returns_unhealthy(self):
        result = smoke_test_deploy("https://invalid.invalid", max_wait=1)
        assert result["healthy"] is False

    @patch('saasclaw_engine.deployments.smoke_tests.urllib.request.urlopen')
    def test_checks_include_root_url(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'<html><body>OK</body></html>'
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        result = smoke_test_deploy("https://example.com", max_wait=1)
        check_names = [c["name"] for c in result["checks"]]
        assert "root_url" in check_names

"""Integration tests against the staging server.

Run from the engine repo:
    python3 -m pytest saasclaw_engine/tests/test_staging_integration.py -v --tb=short
"""

import pytest
import requests

STAGING_URL = "https://staging.saasclaw.ai"


@pytest.fixture(scope="session")
def base():
    return STAGING_URL


# ══════════════════════════════════════════════════════════════════════════
# 1. Static Pages
# ══════════════════════════════════════════════════════════════════════════

class TestPublicPages:
    @pytest.mark.parametrize("path", [
        "/", "/blog/", "/tos/", "/privacy/", "/demos/",
    ])
    def test_page_200(self, base, path):
        r = requests.get(f"{base}{path}", timeout=10)
        assert r.status_code == 200, f"{path} → {r.status_code}"

    def test_homepage_has_content(self, base):
        r = requests.get(base, timeout=10)
        assert "SaaSClaw" in r.text

    def test_homepage_has_css(self, base):
        r = requests.get(base, timeout=10)
        assert "/static/" in r.text or "css" in r.text.lower()

    def test_nonexistent_page_404(self, base):
        r = requests.get(f"{base}/this-does-not-exist/", timeout=10)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# 2. Blog
# ══════════════════════════════════════════════════════════════════════════

class TestBlog:
    def test_index_200(self, base):
        r = requests.get(f"{base}/blog/", timeout=10)
        assert r.status_code == 200

    @pytest.mark.parametrize("slug", [
        "ai-coding-agents-copyright-plagiarism-law",
        "pii-ai-privacy-lawsuits-legislation",
        "sending-ssn-pii-through-ai-providers-legal-risks",
        "enterprise-pii-protection-ai-app-builder",
    ])
    def test_post_200(self, base, slug):
        r = requests.get(f"{base}/blog/{slug}/", timeout=10)
        assert r.status_code == 200, f"/blog/{slug}/ → {r.status_code}"

    def test_nonexistent_post_404(self, base):
        r = requests.get(f"{base}/blog/nonexistent/", timeout=10)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# 3. Legal
# ══════════════════════════════════════════════════════════════════════════

class TestLegal:
    def test_tos_content(self, base):
        r = requests.get(f"{base}/tos/", timeout=10)
        assert "Terms" in r.text

    def test_privacy_content(self, base):
        r = requests.get(f"{base}/privacy/", timeout=10)
        assert "Privacy" in r.text

    def test_privacy_mentions_pii(self, base):
        r = requests.get(f"{base}/privacy/", timeout=10)
        assert "PII" in r.text or "personal data" in r.text.lower()


# ══════════════════════════════════════════════════════════════════════════
# 4. Auth
# ══════════════════════════════════════════════════════════════════════════

class TestAuth:
    def test_login_page_200(self, base):
        r = requests.get(f"{base}/login/", timeout=10)
        assert r.status_code == 200

    def test_studio_redirects_without_auth(self, base):
        r = requests.get(f"{base}/studio/", timeout=10, allow_redirects=False)
        assert r.status_code in (301, 302)
        assert "/login" in r.headers.get("Location", "")


# ══════════════════════════════════════════════════════════════════════════
# 5. Static Assets
# ══════════════════════════════════════════════════════════════════════════

class TestStaticAssets:
    def test_css_loads(self, base):
        r = requests.get(f"{base}/static/css/saasclaw.css", timeout=10)
        assert r.status_code == 200
        assert "css" in r.headers.get("content-type", "")

    def test_nonexistent_static_404(self, base):
        r = requests.get(f"{base}/static/nope.js", timeout=10)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# 6. Security
# ══════════════════════════════════════════════════════════════════════════

class TestSecurity:
    def test_https_only(self, base):
        assert base.startswith("https://")

    def test_no_open_redirect_on_root(self, base):
        r = requests.get(f"{base}/", timeout=10, allow_redirects=False)
        if r.status_code in (301, 302):
            loc = r.headers.get("Location", "")
            assert "saasclaw.ai" in loc or loc.startswith("/")

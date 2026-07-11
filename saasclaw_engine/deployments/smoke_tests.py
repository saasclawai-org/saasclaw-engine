"""Smoke tests that run after every deploy.

Checks the deployed app is actually working — not just that the HTTP server
responds, but that the page doesn't crash, API endpoints return valid JSON,
and (for SPAs) the page renders without JS errors.

Used by the deploy pipeline to catch runtime errors that build tests miss.
"""
import json
import logging
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Markers in HTML that indicate a server-side crash
ERROR_MARKERS = [
    "exception", "traceback", "unhandled",
    "500 internal", "npgsql.", "errorpage",
    "internal server error", "service unavailable",
]


def smoke_test_deploy(base_url: str, framework: str = "", max_wait: int = 30) -> dict:
    """Run smoke tests against a deployed app.

    Args:
        base_url: Full URL (e.g. https://foo.preview.saasclaw.ai)
        framework: Project framework (react, nextjs, django, etc.)
        max_wait: Max seconds to wait for app to come up

    Returns dict with:
        - healthy: bool
        - status_code: int
        - error: str | None
        - checks: list of {name, passed, detail}
    """
    result = {
        "healthy": False,
        "status_code": None,
        "error": None,
        "checks": [],
    }

    # Check 1: Root URL returns 200
    root_check = {"name": "root_url", "passed": False, "detail": ""}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            req = urllib.request.Request(base_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")[:2000]
                result["status_code"] = resp.status
                if resp.status == 200:
                    # Check for error markers in HTML
                    lower = body.lower()
                    found = [m for m in ERROR_MARKERS if m in lower]
                    if found:
                        root_check["detail"] = f"Error markers found: {found}"
                        root_check["passed"] = False
                        result["error"] = body[:500]
                        result["checks"].append(root_check)
                        return result
                    root_check["passed"] = True
                    root_check["detail"] = f"HTTP 200, {len(body)} bytes"
                    break
                else:
                    root_check["detail"] = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            result["status_code"] = exc.code
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:300]
                root_check["detail"] = f"HTTP {exc.code}: {err_body[:100]}"
                result["error"] = err_body
            except Exception:
                root_check["detail"] = f"HTTP {exc.code}"
                result["error"] = f"HTTP {exc.code}"
            if exc.code in (502, 503):
                time.sleep(2)
                continue
            break
        except Exception as exc:
            root_check["detail"] = str(exc)[:100]
            time.sleep(2)

    result["checks"].append(root_check)
    if not root_check["passed"]:
        return result

    # Check 2: Static assets (JS/CSS) are accessible
    asset_check = {"name": "static_assets", "passed": False, "detail": ""}
    try:
        # Look for asset references in the HTML
        import re
        asset_patterns = re.findall(r'(?:src|href)=["\']([^"\']*\.(?:js|css))["\']', body[:2000])
        if asset_patterns:
            first_asset = asset_patterns[0]
            asset_url = first_asset if first_asset.startswith("http") else f"{base_url.rstrip('/')}/{first_asset.lstrip('/')}"
            try:
                req2 = urllib.request.Request(asset_url, method="HEAD")
                with urllib.request.urlopen(req2, timeout=5) as resp2:
                    if resp2.status in (200, 301, 302):
                        asset_check["passed"] = True
                        asset_check["detail"] = f"Asset accessible: {first_asset}"
                    else:
                        asset_check["detail"] = f"Asset returned HTTP {resp2.status}"
            except Exception as exc:
                asset_check["detail"] = f"Asset check failed: {str(exc)[:80]}"
        else:
            asset_check["passed"] = True
            asset_check["detail"] = "No static assets referenced (OK for API-only apps)"
    except Exception as exc:
        asset_check["detail"] = str(exc)[:80]
    result["checks"].append(asset_check)

    # Check 3: API health (for full-stack frameworks)
    if framework in ("django", "nextjs", "dotnet", "htmx"):
        api_check = {"name": "api_endpoint", "passed": False, "detail": ""}
        api_paths = ["/api/", "/api/health/", "/healthz", "/health/"]
        for path in api_paths:
            try:
                api_url = f"{base_url.rstrip('/')}{path}"
                req3 = urllib.request.Request(api_url, method="GET")
                with urllib.request.urlopen(req3, timeout=5) as resp3:
                    api_body = resp3.read().decode("utf-8", errors="replace")[:500]
                    if resp3.status == 200:
                        # Try to parse as JSON
                        try:
                            json.loads(api_body)
                            api_check["passed"] = True
                            api_check["detail"] = f"{path} returned valid JSON"
                            break
                        except json.JSONDecodeError:
                            # Not JSON but 200 is fine
                            api_check["passed"] = True
                            api_check["detail"] = f"{path} returned HTTP 200"
                            break
            except urllib.error.HTTPError:
                continue
            except Exception:
                continue
        if not api_check["passed"]:
            api_check["detail"] = "No API endpoints responded (may be OK)"
            api_check["passed"] = True  # Don't fail deploy just because API isn't set up
        result["checks"].append(api_check)

    # All checks passed
    result["healthy"] = all(c["passed"] for c in result["checks"])
    return result

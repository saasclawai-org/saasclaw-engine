"""Tests for the security scan tool, model, and API endpoints."""

import json
from datetime import timedelta
from unittest.mock import patch, MagicMock

import pytest
from django.test import TestCase, Client, RequestFactory
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from saasclaw_engine.agent.models import SecurityScanResult
from saasclaw_engine.agent.tools import (
    _security_scan_tool,
    _quick_scan_instructions,
    _full_scan_instructions,
    execute_tool,
)
from saasclaw_engine.agent.tool_subtasks import TOOL_DEFINITIONS
from saasclaw_engine.projects.models import Project

User = get_user_model()


# ─── Tool Definition Tests ────────────────────────────────────────────

class TestSecurityScanToolDefinition:
    """Verify the security_scan tool is properly registered."""

    def test_tool_in_definitions(self):
        names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
        assert "security_scan" in names

    def test_tool_has_scope_parameter(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "security_scan")
        props = tool["function"]["parameters"]["properties"]
        assert "scope" in props

    def test_tool_scope_enum_values(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "security_scan")
        enum = tool["function"]["parameters"]["properties"]["scope"]["enum"]
        assert set(enum) == {"full", "quick", "recent_changes"}

    def test_tool_scope_default_is_quick(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "security_scan")
        assert tool["function"]["parameters"]["properties"]["scope"]["default"] == "quick"

    def test_tool_has_description(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "security_scan")
        desc = tool["function"]["description"]
        assert len(desc) > 20
        assert "security" in desc.lower() or "vulnerability" in desc.lower()


# ─── Tool Handler Tests ───────────────────────────────────────────────

class TestSecurityScanHandler:
    """Test the _security_scan_tool handler function."""

    def test_quick_scan_returns_string(self):
        result = _security_scan_tool("/tmp", scope="quick")
        assert isinstance(result, str)

    def test_quick_scan_contains_grep_commands(self):
        result = _security_scan_tool("/tmp", scope="quick")
        assert "grep" in result
        assert "SQL Injection" in result
        assert "XSS" in result

    def test_quick_scan_has_all_6_categories(self):
        result = _security_scan_tool("/tmp", scope="quick")
        for category in ["SQL Injection", "XSS", "Hardcoded Secrets",
                         "Permissive CORS", "Path Traversal", "Command Injection"]:
            assert category in result, f"Missing category: {category}"

    def test_full_scan_returns_string(self):
        result = _security_scan_tool("/tmp", scope="full")
        assert isinstance(result, str)

    def test_full_scan_contains_all_phases(self):
        result = _security_scan_tool("/tmp", scope="full")
        assert "PHASE 1: RECONNAISSANCE" in result
        assert "PHASE 2: HUNT" in result
        assert "PHASE 3: DISPROVE" in result
        assert "PHASE 4: REPORT" in result

    def test_full_scan_mentions_severity_levels(self):
        result = _security_scan_tool("/tmp", scope="full")
        assert "CRITICAL" in result
        assert "HIGH" in result
        assert "MEDIUM" in result
        assert "LOW" in result

    def test_full_scan_mentions_vulnerability_types(self):
        result = _security_scan_tool("/tmp", scope="full")
        assert "SQL Injection" in result
        assert "XSS" in result
        assert "Path Traversal" in result
        assert "SSRF" in result
        assert "Command Injection" in result
        assert "Hardcoded Secrets" in result

    def test_recent_changes_scope(self):
        result = _security_scan_tool("/tmp", scope="recent_changes")
        assert "SECURITY AUDIT" in result
        assert "recent" in result.lower() or "last commit" in result.lower()

    def test_unknown_scope_defaults_to_full(self):
        result = _security_scan_tool("/tmp", scope="unknown_mode")
        assert "SECURITY AUDIT" in result

    def test_quick_scan_shorter_than_full(self):
        quick = _security_scan_tool("/tmp", scope="quick")
        full = _security_scan_tool("/tmp", scope="full")
        assert len(quick) < len(full)

    def test_handler_with_session_id(self):
        """session_id parameter should be accepted without error."""
        result = _security_scan_tool("/tmp", scope="quick", session_id="test-123")
        assert isinstance(result, str)


# ─── Execute Tool Integration ─────────────────────────────────────────

class TestExecuteToolSecurityScan:
    """Test that execute_tool properly dispatches security_scan."""

    def test_execute_tool_dispatches_security_scan(self):
        """execute_tool should dispatch security_scan and return instructions."""
        result = execute_tool(
            workspace_path="/tmp",
            name="security_scan",
            args={"scope": "quick"},
            session_id="test-session",
        )
        assert isinstance(result, str)
        assert "QUICK SECURITY SCAN" in result

    def test_execute_tool_security_scan_full(self):
        result = execute_tool(
            workspace_path="/tmp",
            name="security_scan",
            args={"scope": "full"},
        )
        assert "SECURITY AUDIT" in result

    def test_execute_tool_security_scan_default_scope(self):
        """When no scope provided, should default to 'full' (via args.get default)."""
        result = execute_tool(
            workspace_path="/tmp",
            name="security_scan",
            args={},
        )
        # Default in the handler is "full" when scope isn't "quick"
        assert isinstance(result, str)
        assert "SECURITY" in result


# ─── Model Tests ──────────────────────────────────────────────────────

class TestSecurityScanResultModel(TestCase):
    """Test the SecurityScanResult Django model."""

    def setUp(self):
        # Disconnect Penpot auto-provision signal (imports rest_framework)
        from django.db.models.signals import post_save
        post_save.disconnect(dispatch_uid="auto_provision_penpot", sender=User)
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )
        self.project = Project.objects.create(
            slug="test-project",
            name="Test Project",
            framework="react",
            owner=self.user,
        )

    def test_create_scan_result(self):
        scan = SecurityScanResult.objects.create(
            project=self.project,
            scan_type="quick",
            status="completed",
        )
        self.assertEqual(scan.scan_type, "quick")
        self.assertEqual(scan.status, "completed")
        self.assertEqual(scan.total_findings, 0)
        self.assertEqual(scan.critical_count, 0)

    def test_default_status_is_running(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(scan.status, "running")

    def test_default_scan_type_is_full(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(scan.scan_type, "full")

    def test_str_representation(self):
        scan = SecurityScanResult.objects.create(
            project=self.project,
            scan_type="quick",
            status="completed",
        )
        self.assertIn("test-project", str(scan))
        self.assertIn("quick", str(scan))
        self.assertIn("completed", str(scan))

    def test_summary_property_no_findings(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(scan.summary, "✅ No issues")

    def test_summary_property_with_findings(self):
        scan = SecurityScanResult.objects.create(
            project=self.project,
            critical_count=2,
            high_count=3,
            medium_count=1,
        )
        summary = scan.summary
        self.assertIn("🔴 2", summary)
        self.assertIn("🟠 3", summary)
        self.assertIn("🟡 1", summary)

    def test_summary_property_low_only(self):
        scan = SecurityScanResult.objects.create(
            project=self.project,
            low_count=5,
        )
        self.assertIn("🔵 5", scan.summary)

    def test_findings_json_default_is_empty_list(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(scan.findings_json, [])

    def test_findings_json_can_store_list(self):
        findings = [
            {"severity": "critical", "file": "app.py", "line": 42, "issue": "SQL injection"},
            {"severity": "high", "file": "views.py", "line": 100, "issue": "XSS"},
        ]
        scan = SecurityScanResult.objects.create(
            project=self.project,
            findings_json=findings,
        )
        scan.refresh_from_db()
        self.assertEqual(len(scan.findings_json), 2)
        self.assertEqual(scan.findings_json[0]["severity"], "critical")

    def test_raw_output_blank_default(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(scan.raw_output, "")

    def test_session_id_blank_default(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(scan.session_id, "")

    def test_completed_at_null_by_default(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertIsNone(scan.completed_at)

    def test_created_at_auto_set(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.assertIsNotNone(scan.created_at)

    def test_ordering_newest_first(self):
        scan1 = SecurityScanResult.objects.create(project=self.project)
        # Need to manipulate created_at since auto_now_add uses same timestamp
        SecurityScanResult.objects.filter(pk=scan1.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        scan2 = SecurityScanResult.objects.create(project=self.project)
        scans = list(SecurityScanResult.objects.all())
        self.assertEqual(scans[0], scan2)
        self.assertEqual(scans[1], scan1)

    def test_cascade_delete_with_project(self):
        scan = SecurityScanResult.objects.create(project=self.project)
        self.project.delete()
        self.assertFalse(SecurityScanResult.objects.filter(pk=scan.pk).exists())

    def test_related_name_security_scans(self):
        SecurityScanResult.objects.create(project=self.project)
        SecurityScanResult.objects.create(project=self.project)
        self.assertEqual(self.project.security_scans.count(), 2)


# ─── API Endpoint Tests ───────────────────────────────────────────────

class TestSecurityScanAPI(TestCase):
    """Test the security scan REST API endpoints using RequestFactory."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Mock rest_framework since it's only in the app venv, not engine venv
        import sys
        import types
        if 'rest_framework' not in sys.modules:
            def api_view(methods):
                def decorator(func):
                    return func
                return decorator
            
            def permission_classes(perms):
                def decorator(func):
                    return func
                return decorator
            
            class Response:
                def __init__(self, data, status=None, content_type=None):
                    self.data = data
                    self.status_code = status or 200
                    self.content = json.dumps(data).encode() if isinstance(data, (dict, list)) else str(data).encode()
                    self.content_type = content_type or 'application/json'
            
            class IsAuthenticated:
                pass
            
            rf = types.ModuleType('rest_framework')
            rf.__path__ = []
            rf.__package__ = 'rest_framework'
            
            rf_decorators = types.ModuleType('rest_framework.decorators')
            rf_decorators.api_view = api_view
            rf_decorators.permission_classes = permission_classes
            
            rf_response = types.ModuleType('rest_framework.response')
            rf_response.Response = Response
            
            rf_permissions = types.ModuleType('rest_framework.permissions')
            rf_permissions.IsAuthenticated = IsAuthenticated
            
            rf_status = types.ModuleType('rest_framework.status')
            rf_status.HTTP_200_OK = 200
            rf_status.HTTP_201_CREATED = 201
            rf_status.HTTP_400_BAD_REQUEST = 400
            rf_status.HTTP_401_UNAUTHORIZED = 401
            rf_status.HTTP_403_FORBIDDEN = 403
            rf_status.HTTP_404_NOT_FOUND = 404
            rf_status.HTTP_500_INTERNAL_SERVER_ERROR = 500
            
            rf.decorators = rf_decorators
            rf.response = rf_response
            rf.permissions = rf_permissions
            rf.status = rf_status
            
            sys.modules['rest_framework'] = rf
            sys.modules['rest_framework.decorators'] = rf_decorators
            sys.modules['rest_framework.response'] = rf_response
            sys.modules['rest_framework.permissions'] = rf_permissions
            sys.modules['rest_framework.status'] = rf_status

    def setUp(self):
        self.rf = RequestFactory()
        # Disconnect Penpot auto-provision signal (imports rest_framework)
        from django.db.models.signals import post_save
        post_save.disconnect(dispatch_uid="auto_provision_penpot", sender=User)
        self.user = User.objects.create_user(
            username="apiuser", password="testpass123"
        )
        self.project = Project.objects.create(
            slug="api-test-project",
            name="API Test Project",
            framework="react",
            owner=self.user,
        )

    def _make_request(self, method, path, data=None, user=None):
        """Create a request with optional authenticated user."""
        if method == "GET":
            request = self.rf.get(path)
        elif method == "POST":
            request = self.rf.post(path, data=json.dumps(data or {}), content_type="application/json")
        request.user = user or AnonymousUser()
        return request

    def test_list_scans_empty(self):
        from saasclaw_engine.public_api.security_views import list_security_scans
        request = self._make_request("GET", "/api/v1/projects/api-test-project/security/scans/")
        request.user = MagicMock(is_authenticated=True)
        response = list_security_scans(request, slug="api-test-project")
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["results"] == []

    def test_list_scans_with_data(self):
        from saasclaw_engine.public_api.security_views import list_security_scans
        SecurityScanResult.objects.create(
            project=self.project,
            scan_type="quick",
            status="completed",
            critical_count=1,
            high_count=2,
        )
        request = self._make_request("GET", "/api/v1/projects/api-test-project/security/scans/")
        request.user = MagicMock(is_authenticated=True)
        response = list_security_scans(request, slug="api-test-project")
        assert response.status_code == 200
        data = json.loads(response.content)
        assert len(data["results"]) == 1
        assert data["results"][0]["critical_count"] == 1

    def test_list_scans_missing_project_404(self):
        from saasclaw_engine.public_api.security_views import list_security_scans
        request = self._make_request("GET", "/api/v1/projects/nonexistent/security/scans/")
        request.user = MagicMock(is_authenticated=True)
        response = list_security_scans(request, slug="nonexistent")
        assert response.status_code == 404

    def test_trigger_scan_creates_record(self):
        from saasclaw_engine.public_api.security_views import trigger_security_scan
        request = self._make_request("POST", "/api/v1/projects/api-test-project/security/scan/",
                                     data={"scan_type": "quick"})
        request.user = MagicMock(is_authenticated=True)
        request.data = {"scan_type": "quick"}  # DRF adds .data from body
        response = trigger_security_scan(request, slug="api-test-project")
        assert response.status_code == 201
        data = json.loads(response.content)
        assert data["status"] == "running"
        assert data["scan_type"] == "quick"
        assert SecurityScanResult.objects.filter(pk=data["id"]).exists()

    def test_trigger_scan_default_type(self):
        from saasclaw_engine.public_api.security_views import trigger_security_scan
        request = self._make_request("POST", "/api/v1/projects/api-test-project/security/scan/",
                                     data={})
        request.user = MagicMock(is_authenticated=True)
        request.data = {}  # DRF adds .data from body
        response = trigger_security_scan(request, slug="api-test-project")
        assert response.status_code == 201
        data = json.loads(response.content)
        assert data["scan_type"] == "quick"

    def test_scan_detail(self):
        from saasclaw_engine.public_api.security_views import security_scan_detail
        scan = SecurityScanResult.objects.create(
            project=self.project,
            scan_type="full",
            status="completed",
            critical_count=1,
            findings_json=[{"severity": "critical", "file": "app.py"}],
            raw_output="Found 1 critical issue",
        )
        request = self._make_request("GET", f"/api/v1/projects/api-test-project/security/scans/{scan.id}/")
        request.user = MagicMock(is_authenticated=True)
        response = security_scan_detail(request, slug="api-test-project", scan_id=scan.id)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["critical_count"] == 1
        assert data["scan_type"] == "full"
        assert len(data["findings"]) == 1
        assert data["raw_output"] == "Found 1 critical issue"

    def test_scan_detail_not_found(self):
        from saasclaw_engine.public_api.security_views import security_scan_detail
        request = self._make_request("GET", "/api/v1/projects/api-test-project/security/scans/99999/")
        request.user = MagicMock(is_authenticated=True)
        response = security_scan_detail(request, slug="api-test-project", scan_id=99999)
        assert response.status_code == 404

    def test_scan_detail_wrong_project_404(self):
        """Scan from one project shouldn't be accessible via another project's slug."""
        from saasclaw_engine.public_api.security_views import security_scan_detail
        other_project = Project.objects.create(slug="other-project", name="Other", framework="vue", owner=self.user)
        scan = SecurityScanResult.objects.create(project=other_project)
        request = self._make_request("GET", f"/api/v1/projects/api-test-project/security/scans/{scan.id}/")
        request.user = MagicMock(is_authenticated=True)
        response = security_scan_detail(request, slug="api-test-project", scan_id=scan.id)
        assert response.status_code == 404


# ─── Instruction Content Quality Tests ────────────────────────────────

class TestScanInstructionQuality:
    """Verify scan instructions contain actionable content."""

    def test_quick_scan_has_runnable_grep_commands(self):
        result = _quick_scan_instructions()
        # Should contain actual grep commands that can be run
        assert result.count("grep -rn") >= 6  # At least 6 grep commands

    def test_quick_scan_covers_sql_injection(self):
        result = _quick_scan_instructions()
        assert "execute" in result or "query" in result
        assert "SELECT" in result

    def test_quick_scan_covers_xss(self):
        result = _quick_scan_instructions()
        assert "dangerouslySetInnerHTML" in result
        assert "innerHTML" in result

    def test_quick_scan_covers_hardcoded_secrets(self):
        result = _quick_scan_instructions()
        assert "api_key" in result.lower() or "sk-" in result
        assert "password" in result.lower()

    def test_full_scan_has_exclusion_list(self):
        result = _full_scan_instructions("full")
        assert "node_modules" in result
        assert "__pycache__" in result
        assert ".git" in result

    def test_full_scan_has_entry_point_enumeration(self):
        result = _full_scan_instructions("full")
        assert "ENTRY POINTS" in result or "entry point" in result.lower()
        assert "req.body" in result or "request.GET" in result

    def test_full_scan_has_sink_enumeration(self):
        result = _full_scan_instructions("full")
        assert "SINK" in result or "sink" in result.lower()
        assert "exec(" in result or "execute(" in result

    def test_full_scan_has_disprove_phase(self):
        result = _full_scan_instructions("full")
        assert "DISPROVE" in result
        assert "false positive" in result.lower() or "not exploitable" in result.lower()

    def test_full_scan_has_report_format(self):
        result = _full_scan_instructions("full")
        assert "SEVERITY" in result
        assert "Location" in result
        assert "Entry Point" in result
        assert "Data Flow" in result
        assert "Impact" in result
        assert "Fix" in result

    def test_full_scan_scope_description(self):
        full_result = _full_scan_instructions("full")
        assert "ENTIRE codebase" in full_result

    def test_recent_changes_scope_description(self):
        result = _full_scan_instructions("recent_changes")
        assert "last commit" in result.lower() or "changed" in result.lower()

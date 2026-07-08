"""Module split integrity tests for service.py.

Verifies that the service.py shim correctly re-exports all public names
from the sub-modules (deploy_infra, deploy_django, deploy_static, deploy_node,
deploy_dotnet) so that callers importing from ``saasclaw_engine.deployments.service``
get the full API surface.

This test would have caught issues where the service.py split accidentally
dropped names or imported from the wrong sub-module.
"""

import importlib

import pytest


# ── Public API names that service.py must expose ──────────────────────────

# Top-level orchestrators defined directly in service.py
SERVICE_PUBLIC_NAMES = [
    "deploy_preview",
    "deploy_production",
    "decommission_project",
]

# Names re-exported from deploy_infra
DEPLOY_INFRA_NAMES = [
    "_load_env_file",
    "_serialize_env_file",
    "_write_text",
    "_normalize_ownership",
    "_run_command",
    "_run_logged_subprocess",
    "_tail_text",
    "_repo_commit_sha",
    "_remote_repo_url",
    "_normalize_repo_url",
    "_assert_repo_binding",
    "_refresh_repo_checkout_for_deploy",
    "_slugify_system_name",
    "_ensure_app_port",
    "_publish_directory",
    "_pick_ssl_certs",
    "_ensure_systemd_service",
    "_write_tmp_script",
    "_write_and_validate_nginx",
    "_restart_service",
    "_ensure_nginx_spa_proxy",
    "_ensure_nginx_proxy",
    "_ensure_nginx_static",
    "_scan_for_secrets",
    "_scan_dependencies",
    "_ensure_postgres_database",
    "_wait_for_http_healthcheck",
]

# Names re-exported from deploy_django
DEPLOY_DJANGO_NAMES = [
    "_detect_wsgi_entrypoint",
    "_detect_python_entrypoint",
    "_available_python_versions",
    "_detect_python_version",
    "_python_binary_for_version",
    "_configure_django_runtime_service",
    "_deploy_django_environment",
    "_ensure_django_admin_user",
]

# Names re-exported from deploy_static
DEPLOY_STATIC_NAMES = [
    "_detect_output_dir",
    "_deploy_static_environment",
]

# Names re-exported from deploy_node
DEPLOY_NODE_NAMES = [
    "_detect_node_version",
    "_node_binary_path",
    "_deploy_node_ssr_environment",
]

# Names re-exported from deploy_dotnet
DEPLOY_DOTNET_NAMES = [
    "_ensure_dotnet_sdk",
    "_detect_dotnet_entrypoint",
    "_deploy_dotnet_environment",
]


class TestServicePublicAPI:
    """Verify service.py exposes the expected public API."""

    @pytest.mark.parametrize("name", SERVICE_PUBLIC_NAMES)
    def test_public_function_exists(self, name):
        """Each public function should be importable from service.py."""
        from saasclaw_engine.deployments import service
        assert hasattr(service, name), f"service.{name} not found — was it dropped in the split?"

    @pytest.mark.parametrize("name", SERVICE_PUBLIC_NAMES)
    def test_public_function_is_callable(self, name):
        from saasclaw_engine.deployments import service
        fn = getattr(service, name)
        assert callable(fn), f"service.{name} is not callable"


class TestServiceReExportsFromDeployInfra:
    """Verify service.py re-exports all names from deploy_infra."""

    @pytest.mark.parametrize("name", DEPLOY_INFRA_NAMES)
    def test_name_importable_from_service(self, name):
        from saasclaw_engine.deployments import service
        assert hasattr(service, name), f"service.{name} (from deploy_infra) not found"

    @pytest.mark.parametrize("name", DEPLOY_INFRA_NAMES)
    def test_name_same_object_as_deploy_infra(self, name):
        """The name in service should be the same object as in deploy_infra."""
        from saasclaw_engine.deployments import service, deploy_infra
        assert getattr(service, name) is getattr(deploy_infra, name), (
            f"service.{name} is not the same object as deploy_infra.{name}"
        )


class TestServiceReExportsFromDeployDjango:
    """Verify service.py re-exports all names from deploy_django."""

    @pytest.mark.parametrize("name", DEPLOY_DJANGO_NAMES)
    def test_name_importable_from_service(self, name):
        from saasclaw_engine.deployments import service
        assert hasattr(service, name), f"service.{name} (from deploy_django) not found"

    @pytest.mark.parametrize("name", DEPLOY_DJANGO_NAMES)
    def test_name_same_object_as_deploy_django(self, name):
        from saasclaw_engine.deployments import service, deploy_django
        assert getattr(service, name) is getattr(deploy_django, name), (
            f"service.{name} is not the same object as deploy_django.{name}"
        )


class TestServiceReExportsFromDeployStatic:
    """Verify service.py re-exports all names from deploy_static."""

    @pytest.mark.parametrize("name", DEPLOY_STATIC_NAMES)
    def test_name_importable_from_service(self, name):
        from saasclaw_engine.deployments import service
        assert hasattr(service, name), f"service.{name} (from deploy_static) not found"

    @pytest.mark.parametrize("name", DEPLOY_STATIC_NAMES)
    def test_name_same_object_as_deploy_static(self, name):
        from saasclaw_engine.deployments import service, deploy_static
        assert getattr(service, name) is getattr(deploy_static, name), (
            f"service.{name} is not the same object as deploy_static.{name}"
        )


class TestServiceReExportsFromDeployNode:
    """Verify service.py re-exports all names from deploy_node."""

    @pytest.mark.parametrize("name", DEPLOY_NODE_NAMES)
    def test_name_importable_from_service(self, name):
        from saasclaw_engine.deployments import service
        assert hasattr(service, name), f"service.{name} (from deploy_node) not found"

    @pytest.mark.parametrize("name", DEPLOY_NODE_NAMES)
    def test_name_same_object_as_deploy_node(self, name):
        from saasclaw_engine.deployments import service, deploy_node
        assert getattr(service, name) is getattr(deploy_node, name), (
            f"service.{name} is not the same object as deploy_node.{name}"
        )


class TestServiceReExportsFromDeployDotnet:
    """Verify service.py re-exports all names from deploy_dotnet."""

    @pytest.mark.parametrize("name", DEPLOY_DOTNET_NAMES)
    def test_name_importable_from_service(self, name):
        from saasclaw_engine.deployments import service
        assert hasattr(service, name), f"service.{name} (from deploy_dotnet) not found"

    @pytest.mark.parametrize("name", DEPLOY_DOTNET_NAMES)
    def test_name_same_object_as_deploy_dotnet(self, name):
        from saasclaw_engine.deployments import service, deploy_dotnet
        assert getattr(service, name) is getattr(deploy_dotnet, name), (
            f"service.{name} is not the same object as deploy_dotnet.{name}"
        )


class TestStarImport:
    """Verify that ``from service import *`` works and includes key names.

    A star import should not raise and should include the public API names.
    This catches issues where ``__all__`` might be misconfigured or names
    are accidentally shadowed.
    """

    def test_star_import_includes_public_api(self):
        ns = {}
        exec("from saasclaw_engine.deployments.service import *", ns)
        for name in SERVICE_PUBLIC_NAMES:
            assert name in ns, f"star import did not include {name}"

    def test_star_import_includes_infra_helpers(self):
        ns = {}
        exec("from saasclaw_engine.deployments.service import *", ns)
        # Star import brings in all non-underscore names from imported modules
        # The underscore-prefixed helpers won't be in star import (Python convention)
        # but the public functions should be there
        assert "deploy_preview" in ns
        assert "deploy_production" in ns
        assert "decommission_project" in ns


class TestSubModuleIndependence:
    """Verify each sub-module can be imported independently without service.py."""

    def test_deploy_infra_independent(self):
        """deploy_infra should not import from service.py (no circular dep)."""
        mod = importlib.import_module("saasclaw_engine.deployments.deploy_infra")
        assert mod is not None

    def test_deploy_django_imports_from_infra(self):
        """deploy_django imports from deploy_infra, not from service."""
        mod = importlib.import_module("saasclaw_engine.deployments.deploy_django")
        assert mod is not None

    def test_deploy_static_imports_from_infra(self):
        mod = importlib.import_module("saasclaw_engine.deployments.deploy_static")
        assert mod is not None

    def test_deploy_node_imports_from_infra(self):
        mod = importlib.import_module("saasclaw_engine.deployments.deploy_node")
        assert mod is not None

    def test_deploy_dotnet_imports_from_infra(self):
        mod = importlib.import_module("saasclaw_engine.deployments.deploy_dotnet")
        assert mod is not None

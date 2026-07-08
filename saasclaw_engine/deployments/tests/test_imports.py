"""Import validation tests for all deployment modules.

Catches missing imports (e.g. the missing ``secrets`` and ``Path`` imports
that were lost during the service.py split) by importing each module
individually and asserting no ImportError or NameError occurs.
"""

import importlib

import pytest


DEPLOY_MODULES = [
    "saasclaw_engine.deployments.deploy_infra",
    "saasclaw_engine.deployments.deploy_django",
    "saasclaw_engine.deployments.deploy_static",
    "saasclaw_engine.deployments.deploy_node",
    "saasclaw_engine.deployments.deploy_dotnet",
    "saasclaw_engine.deployments.service",
]


@pytest.mark.parametrize("module_name", DEPLOY_MODULES)
def test_module_imports_cleanly(module_name):
    """Each deployment module should import without ImportError or NameError."""
    mod = importlib.import_module(module_name)
    assert mod is not None


@pytest.mark.parametrize("module_name", DEPLOY_MODULES)
def test_module_has_logger(module_name):
    """Each deployment module should set up a module-level logger."""
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "logger"), f"{module_name} missing module-level logger"


class TestDeployInfraImports:
    """Verify deploy_infra has all stdlib imports it needs."""

    def test_secrets_available(self):
        """deploy_infra uses ``secrets`` for token/password generation."""
        from saasclaw_engine.deployments import deploy_infra
        assert hasattr(deploy_infra, "secrets")

    def test_path_available(self):
        """deploy_infra uses ``Path`` throughout for filesystem paths."""
        from saasclaw_engine.deployments import deploy_infra
        assert hasattr(deploy_infra, "Path")

    def test_subprocess_available(self):
        from saasclaw_engine.deployments import deploy_infra
        assert hasattr(deploy_infra, "subprocess")

    def test_shutil_available(self):
        from saasclaw_engine.deployments import deploy_infra
        assert hasattr(deploy_infra, "shutil")


class TestDeployDjangoImports:
    """Verify deploy_django has all imports from the split."""

    def test_secrets_available(self):
        """deploy_django uses ``secrets`` for DB password generation."""
        from saasclaw_engine.deployments import deploy_django
        # imported as ``secrets as _secrets``
        assert hasattr(deploy_django, "_secrets")

    def test_path_available(self):
        from saasclaw_engine.deployments import deploy_django
        assert hasattr(deploy_django, "Path")

    def test_subprocess_available(self):
        from saasclaw_engine.deployments import deploy_django
        assert hasattr(deploy_django, "subprocess")


class TestDeployStaticImports:
    """Verify deploy_static has all imports from the split."""

    def test_secrets_available(self):
        from saasclaw_engine.deployments import deploy_static
        assert hasattr(deploy_static, "secrets")

    def test_path_available(self):
        from saasclaw_engine.deployments import deploy_static
        assert hasattr(deploy_static, "Path")


class TestDeployNodeImports:
    """Verify deploy_node has all imports from the split."""

    def test_path_available(self):
        from saasclaw_engine.deployments import deploy_node
        assert hasattr(deploy_node, "Path")

    def test_subprocess_available(self):
        from saasclaw_engine.deployments import deploy_node
        assert hasattr(deploy_node, "subprocess")


class TestDeployDotnetImports:
    """Verify deploy_dotnet has all imports from the split."""

    def test_path_available(self):
        from saasclaw_engine.deployments import deploy_dotnet
        assert hasattr(deploy_dotnet, "Path")

    def test_subprocess_available(self):
        from saasclaw_engine.deployments import deploy_dotnet
        assert hasattr(deploy_dotnet, "subprocess")


class TestServiceShimImports:
    """Verify the service.py shim re-imports everything it needs from sub-modules."""

    def test_secrets_available(self):
        """service.py needs ``secrets`` for decommission token generation."""
        from saasclaw_engine.deployments import service
        assert hasattr(service, "secrets")

    def test_path_available(self):
        """service.py needs ``Path`` for filesystem operations."""
        from saasclaw_engine.deployments import service
        assert hasattr(service, "Path")

    def test_subprocess_available(self):
        from saasclaw_engine.deployments import service
        assert hasattr(service, "subprocess")

    def test_shutil_available(self):
        from saasclaw_engine.deployments import service
        assert hasattr(service, "shutil")

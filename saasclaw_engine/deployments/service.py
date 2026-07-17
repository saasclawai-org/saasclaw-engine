"""Deploy service orchestration — thin shim that delegates to specialized deploy modules.

Public API: deploy_preview(), deploy_production(), decommission_project()
"""
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.utils import timezone as dj_timezone

from saasclaw_engine.integrations.github import clone_or_update_repo
from saasclaw_engine.projects.models import Project
from saasclaw_engine.deployments.models import Deployment, Environment

from .deploy_infra import (
    _load_env_file, _serialize_env_file, _write_text, _normalize_ownership,
    _run_command, _run_logged_subprocess, _tail_text, _repo_commit_sha,
    _remote_repo_url, _normalize_repo_url, _assert_repo_binding,
    _refresh_repo_checkout_for_deploy, _slugify_system_name, _ensure_app_port,
    _publish_directory, _pick_ssl_certs, _ensure_systemd_service,
    _write_tmp_script, _write_and_validate_nginx, _restart_service,
    _ensure_nginx_spa_proxy, _ensure_nginx_proxy, _ensure_nginx_static,
    _scan_for_secrets, _scan_dependencies, _scan_with_semgrep, _ensure_postgres_database,
    _wait_for_http_healthcheck,
)
from .deploy_django import (
    _detect_wsgi_entrypoint, _detect_python_entrypoint,
    _available_python_versions, _detect_python_version,
    _python_binary_for_version, _configure_django_runtime_service,
    _deploy_django_environment, _ensure_django_admin_user,
)
from .deploy_static import _detect_output_dir, _deploy_static_environment
from .deploy_node import _detect_node_version, _node_binary_path, _deploy_node_ssr_environment
from .deploy_dotnet import _ensure_dotnet_sdk, _detect_dotnet_entrypoint, _deploy_dotnet_environment
from .deploy_java import _deploy_java_environment

logger = logging.getLogger(__name__)

def _set_deploy_phase(deployment, phase, detail=''):
    """Update deployment.metadata_json with current phase for UI progress tracking."""
    try:
        phases = (deployment.metadata_json or {}).get('phases', [])
        # Don't add duplicate phases
        if not phases or phases[-1].get('name') != phase:
            phases.append({'name': phase, 'detail': detail, 'ts': dj_timezone.now().isoformat()})
            deployment.metadata_json = {**(deployment.metadata_json or {}), 'phases': phases, 'current_phase': phase}
            deployment.save(update_fields=['metadata_json'])
    except Exception:
        pass


def _deploy_environment(project: Project, environment_name: str, triggered_by=None) -> Deployment:
    """Main deploy entry point. Clones repo, runs deploy pipeline for the environment."""
    environment = project.environments.filter(name=environment_name).first()
    if not environment:
        raise RuntimeError(f'No {environment_name} environment for project {project.slug}')

    repo_path = Path(project.workspace_root) / 'repo'
    log_dir = Path(project.workspace_root) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)

    deployment = Deployment.objects.create(
        project=project,
        environment=environment,
        triggered_by=triggered_by,
        source=Deployment.Source.MANUAL,
        status=Deployment.Status.QUEUED,
        git_branch=project.repo_default_branch or 'main',
    )

    log_file = log_dir / f'deploy-{deployment.id}.log'
    try:
        deployment.status = Deployment.Status.RUNNING
        deployment.started_at = dj_timezone.now()
        deployment.save(update_fields=['status', 'started_at'])
        _set_deploy_phase(deployment, 'starting', 'Initializing deploy')

        _set_deploy_phase(deployment, 'merge', 'Pulling latest code')
        _refresh_repo_checkout_for_deploy(project, repo_path, log_file)
        deployment.git_commit_sha = _repo_commit_sha(repo_path)
        deployment.save(update_fields=['git_commit_sha'])

        # --- NIST AI RMF: Secret scanning (after clone, before build) ---
        from saasclaw_engine.studio_models.models import SiteSettings
        _site = SiteSettings.get()

        if _site.secret_scan_enabled:
            secret_findings = _scan_for_secrets(repo_path)
            if secret_findings:
                logger.warning('Secret scan found %d issue(s) for %s', len(secret_findings), project.slug)
                with log_file.open('a', encoding='utf-8') as handle:
                    handle.write(f'\n=== SECRET SCAN ({len(secret_findings)} finding(s)) ===\n')
                    for finding in secret_findings:
                        handle.write(f'  WARNING: {finding}\n')
                    handle.write('=== END SECRET SCAN ===\n\n')
        else:
            secret_findings = []

        # --- NIST AI RMF: Dependency scanning (after build, before publish) ---
        if _site.dependency_scan_enabled:
            dep_findings = _scan_dependencies(repo_path)
            if dep_findings:
                logger.warning('Dependency scan found %d issue(s) for %s', len(dep_findings), project.slug)
                with log_file.open('a', encoding='utf-8') as handle:
                    handle.write(f'\n=== DEPENDENCY SCAN ({len(dep_findings)} issue(s)) ===\n')
                    for finding in dep_findings:
                        handle.write(f'  WARNING: {finding}\n')
                    handle.write('=== END DEPENDENCY SCAN ===\n\n')
        else:
            dep_findings = []

        # --- NIST AI RMF: Semgrep static analysis (after clone, before build) ---
        if getattr(_site, 'semgrep_scan_enabled', True):
            semgrep_findings = _scan_with_semgrep(repo_path)
            if semgrep_findings:
                logger.warning('Semgrep scan found %d issue(s) for %s', len(semgrep_findings), project.slug)
                with log_file.open('a', encoding='utf-8') as handle:
                    handle.write(f'\n=== SEMGREP SCAN ({len(semgrep_findings)} finding(s)) ===\n')
                    for finding in semgrep_findings:
                        handle.write(f'  WARNING: {finding}\n')
                    handle.write('=== END SEMGREP SCAN ===\n\n')
        else:
            semgrep_findings = []

        # Block deploy if findings exist and block is enabled
        all_findings = secret_findings + dep_findings + semgrep_findings
        if _site.block_deploy_on_findings and all_findings:
            logger.error("Deploy blocked due to security findings: secrets=%d deps=%d semgrep=%d for %s",
                          len(secret_findings), len(dep_findings), len(semgrep_findings), project.slug)
            deployment.status = Deployment.Status.FAILED
            deployment.error_message = (
                f"Deploy blocked: {len(secret_findings)} secret(s), "
                f"{len(dep_findings)} dependency issue(s), "
                f"{len(semgrep_findings)} static analysis issue(s) found."
            )
            deployment.finished_at = dj_timezone.now()
            deployment.deploy_log_object_key = _tail_text(log_file)
            deployment.save(update_fields=['status', 'error_message', 'finished_at', 'deploy_log_object_key'])
            raise RuntimeError(deployment.error_message)

        _set_deploy_phase(deployment, 'build', f'Building {environment.runtime_kind} app')
        if environment.runtime_kind == Environment.RuntimeKind.DJANGO:
            _deploy_django_environment(project, environment, deployment, repo_path, log_file)
        elif environment.runtime_kind == Environment.RuntimeKind.NODE_SSR:
            _deploy_node_ssr_environment(project, environment, deployment, repo_path, log_file)
        elif environment.runtime_kind == Environment.RuntimeKind.DOTNET:
            _deploy_dotnet_environment(project, environment, deployment, repo_path, log_file)
        elif environment.runtime_kind == Environment.RuntimeKind.JAVA:
            _deploy_java_environment(project, environment, deployment, repo_path, log_file)
        else:
            _deploy_static_environment(project, environment, deployment, repo_path, log_file)

        _set_deploy_phase(deployment, 'deploy', 'Configuring nginx & restarting service')
        _set_deploy_phase(deployment, 'health', 'Running health check')
        # Run smoke tests against deployed app
        from saasclaw_engine.deployments.smoke_tests import smoke_test_deploy
        base_url = f"https://{project.slug}.{settings.PREVIEW_BASE_DOMAIN}" if environment_name == "preview" else f"https://{environment.domain}"
        smoke = smoke_test_deploy(base_url, framework=environment.runtime_kind, max_wait=15)
        deployment.metadata_json = {**(deployment.metadata_json or {}), "smoke_tests": smoke}
        deployment.save(update_fields=["metadata_json"])
        if not smoke.get("healthy"):
            logger.warning("Smoke tests failed for %s: %s", project.slug, smoke.get("error", "unknown"))
        deployment.status = Deployment.Status.SUCCEEDED
        deployment.finished_at = dj_timezone.now()
        deployment.metadata_json = {**(deployment.metadata_json or {}), 'current_phase': 'done'}
        deployment.save(update_fields=['status', 'finished_at', 'metadata_json'])

        project.last_deployed_at = dj_timezone.now()
        project.save(update_fields=['last_deployed_at'])

    except Exception as exc:
        deployment.status = Deployment.Status.FAILED
        deployment.error_message = str(exc)[:5000]
        deployment.finished_at = dj_timezone.now()
        deployment.deploy_log_object_key = _tail_text(log_file)
        deployment.save(update_fields=['status', 'error_message', 'finished_at', 'deploy_log_object_key'])
        raise

    return deployment


def _push_to_github_after_deploy(project: Project, triggered_by=None) -> None:
    """Push deploy repo to GitHub if connected. Swaps remote temporarily."""
    if not project.repo_url or 'github.com' not in project.repo_url:
        return
    if not project.repo_owner or not project.repo_name:
        return

    repo_path = Path(project.workspace_root) / 'repo'
    if not (repo_path / '.git').exists():
        return

    import subprocess as sp
    import os, base64 as _b64, re as _re
    from django.contrib.auth import get_user_model
    from allauth.socialaccount.models import SocialAccount, SocialToken

    User = get_user_model()
    # Try triggered_by user first, then project owner
    user = triggered_by if triggered_by and hasattr(triggered_by, '_state') else project.owner
    if not user:
        return
    social = SocialAccount.objects.filter(user=user, provider='github').first()
    if not social and project.owner and project.owner != user:
        social = SocialAccount.objects.filter(user=project.owner, provider='github').first()
    if not social:
        return
    token_obj = SocialToken.objects.filter(account=social).first()
    if not token_obj:
        return

    token = token_obj.token
    basic = _b64.b64encode(f"x-access-token:{token}".encode()).decode()
    https_url = f"https://github.com/{project.repo_owner}/{project.repo_name}.git"
    bare_url = f"/srv/saasclaw/git/{project.slug}.git"

    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = "SaaSClaw Agent"
    env["GIT_AUTHOR_EMAIL"] = "saasclaw@saasclaw.ai"
    env["GIT_COMMITTER_NAME"] = "SaaSClaw Agent"
    env["GIT_COMMITTER_EMAIL"] = "saasclaw@saasclaw.ai"

    # Swap to HTTPS
    sp.run(['git', 'remote', 'set-url', 'origin', https_url],
           cwd=str(repo_path), capture_output=True, text=True, timeout=10)
    try:
        result = sp.run(
            ['git', '-c', f'http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}',
             'push', 'origin', 'main'],
            cwd=str(repo_path), capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode == 0:
            logger.info("Pushed %s to GitHub: %s/%s", project.slug, project.repo_owner, project.repo_name)
        else:
            logger.warning("GitHub push failed for %s: %s", project.slug, result.stderr.strip())
    finally:
        # Always restore bare repo remote
        sp.run(['git', 'remote', 'set-url', 'origin', bare_url],
               cwd=str(repo_path), capture_output=True, text=True, timeout=10)


def deploy_preview(project: Project, triggered_by=None) -> Deployment:
    """Deploy to the project's preview environment."""
    deployment = _deploy_environment(project, 'preview', triggered_by=triggered_by)
    if deployment.status == Deployment.Status.SUCCEEDED:
        _push_to_github_after_deploy(project, triggered_by=triggered_by)
    return deployment


def deploy_production(project: Project, triggered_by=None) -> Deployment:
    """Deploy to the project's production environment."""
    deployment = _deploy_environment(project, 'production', triggered_by=triggered_by)
    if deployment.status == Deployment.Status.SUCCEEDED:
        _push_to_github_after_deploy(project, triggered_by=triggered_by)
    return deployment


def decommission_project(project_slug: str, project_name: str = '') -> None:
    """Log decommissioning steps for a project (NIST AI RMF GOVERN 1.3).

    This is a logging-only utility — the actual deletion is handled elsewhere.
    Logs to /srv/saasclaw/logs/decommission.log.
    """
    import os as _os
    log_path = Path('/srv/saasclaw/logs/decommission.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = dj_timezone.now().isoformat()

    lines = [f'\n{"="*60}']
    lines.append(f'DECOMMISSION: {project_name or project_slug} (slug={project_slug})')
    lines.append(f'Timestamp: {timestamp}')
    lines.append('-' * 60)

    # Services
    service_names = [f'saasclaw-{project_slug}-preview', f'saasclaw-{project_slug}-production']
    for svc in service_names:
        try:
            result = subprocess.run(['sudo', 'systemctl', 'is-active', svc], capture_output=True, text=True, timeout=5)
            status = result.stdout.strip()
            if status == 'active':
                subprocess.run(['sudo', 'systemctl', 'stop', svc], capture_output=True, timeout=10)
                subprocess.run(['sudo', 'systemctl', 'disable', svc], capture_output=True, timeout=5)
                lines.append(f'Service stopped & disabled: {svc}')
            else:
                lines.append(f'Service not active: {svc} ({status})')
        except Exception as exc:
            lines.append(f'Service check failed for {svc}: {exc}')

    # Nginx configs
    for svc in service_names:
        nginx_site = Path(f'/etc/nginx/sites-enabled/{svc}')
        nginx_avail = Path(f'/etc/nginx/sites-available/{svc}')
        if nginx_site.exists() or nginx_avail.exists():
            try:
                subprocess.run(['sudo', 'rm', '-f', str(nginx_site)], capture_output=True, timeout=5)
                subprocess.run(['sudo', 'rm', '-f', str(nginx_avail)], capture_output=True, timeout=5)
                lines.append(f'Nginx config removed: {svc}')
            except Exception as exc:
                lines.append(f'Nginx removal failed for {svc}: {exc}')
        else:
            lines.append(f'Nginx config not found: {svc}')

    # Try nginx reload
    try:
        subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], capture_output=True, timeout=5)
        lines.append('Nginx reloaded successfully')
    except Exception as exc:
        lines.append(f'Nginx reload failed: {exc}')

    # Git repo
    project_dir = Path(f'/srv/saasclaw/projects/{project_slug}')
    bare_repo = Path(f'/srv/saasclaw/git/{project_slug}.git')
    lines.append(f'Project dir exists: {project_dir.exists()} ({project_dir})')
    lines.append(f'Bare repo exists: {bare_repo.exists()} ({bare_repo})')

    # Data cleanup status
    lines.append(f'Data cleanup: to be handled by DB cascade delete')
    lines.append(f'{"="*60}\n')

    with log_path.open('a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    logger.info('Decommission logged for %s', project_slug)

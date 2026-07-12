"""Static site deployment — extracted from service.py.

Handles Hugo, plain HTML, React SPA, and other static builds.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

from .deploy_infra import (
    _load_env_file, _serialize_env_file, _normalize_ownership, _run_command, _write_text,
    _slugify_system_name, _publish_directory, _ensure_nginx_static,
    _ensure_postgres_database, _pick_ssl_certs,
)
from .deploy_node import _detect_node_version, _node_binary_path

if TYPE_CHECKING:
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.deployments.models import Deployment, Environment

logger = logging.getLogger(__name__)


def _detect_output_dir(repo_path: Path, build_cmd: str = '') -> str:
    """Auto-detect the build output directory for a project.

    Priority:
    1. Explicit config: vite.config.js build.outDir, next.config.js distDir
    2. Known convention: if build uses Vite, default is 'dist'
    3. Fallback scan: check dist/, web/, build/, out/, _site/ — pick first that exists
    4. Final fallback: 'dist'
    """
    import json as _json
    import re as _re

    # --- Check vite.config.js for build.outDir ---
    for cfg_name in ('vite.config.js', 'vite.config.ts', 'vite.config.mts'):
        cfg = repo_path / cfg_name
        if cfg.exists():
            content = cfg.read_text(encoding='utf-8', errors='replace')
            m = _re.search(r"outDir\s*:\s*['\"`]([^'\"`]+)", content)
            if m:
                return m.group(1).strip()
            # Vite default
            return 'dist'

    # --- Check next.config.js for distDir ---
    for cfg_name in ('next.config.js', 'next.config.mjs', 'next.config.ts'):
        cfg = repo_path / cfg_name
        if cfg.exists():
            content = cfg.read_text(encoding='utf-8', errors='replace')
            m = _re.search(r'distDir\s*:\s*[\'"]([^\'"]+)', content)
            if m:
                return m.group(1).strip()
            # Next.js default
            return '.next'

    # --- Check package.json build script for Vite ---
    pkg = repo_path / 'package.json'
    if pkg.exists():
        try:
            data = _json.loads(pkg.read_text())
            scripts = data.get('scripts', {})
            build_script = scripts.get('build', '')
            if 'vite' in build_script.lower():
                return 'dist'
            if 'nuxt' in build_script.lower():
                return '.output/public'
            if 'astro' in build_script.lower():
                return 'dist'
        except Exception:
            pass

    # --- Fallback: pick first directory that exists ---
    candidates = ['dist', 'web', 'build', 'out', '_site', '.next', '.output/public']
    for candidate in candidates:
        if (repo_path / candidate).is_dir():
            return candidate

    return 'dist'


def _deploy_static_environment(project: Project, environment: Environment, deployment: Deployment, repo_path: Path, log_file: Path) -> None:
    """Deploy a static site to an environment."""
    # Provision Postgres database for all projects (including static)
    db_host, db_port = '127.0.0.1', '5432'
    db_suffix = f"_{environment.name}" if environment.name != 'preview' else ''
    db_name = f"saasclaw_{project.slug.replace('-', '_')}{db_suffix}"
    db_user = f"sc_{project.slug.replace('-', '_')}{db_suffix}"[:32]
    db_password = secrets.token_urlsafe(24)

    runtime_root = Path(project.workspace_root) / 'runtime' / environment.name
    runtime_root.mkdir(parents=True, exist_ok=True)
    env_file = runtime_root / '.env'

    _ensure_postgres_database(db_name, db_user, db_password, log_file)

    database_url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

    # Merge repo .env into runtime env (carries JWT keys, API keys, etc.)
    repo_env_file = repo_path / '.env'
    repo_env = _load_env_file(repo_env_file) if repo_env_file.exists() else {}
    existing_env = _load_env_file(env_file)
    env_values = {**existing_env, **repo_env, **{
        'POSTGRES_DB': db_name,
        'POSTGRES_USER': db_user,
        'POSTGRES_PASSWORD': db_password,
        'POSTGRES_HOST': db_host,
        'POSTGRES_PORT': db_port,
        'DATABASE_URL': database_url,
    }}
    _write_text(env_file, _serialize_env_file(env_values))

    build_cmd = environment.build_command or 'echo "No build step"'

    # Determine output_dir: explicit env field (non-empty) > auto-detect > 'dist'
    explicit = getattr(environment, 'output_directory', None) or ''
    if explicit.strip():
        output_dir = explicit.strip()
    else:
        output_dir = _detect_output_dir(repo_path, build_cmd)

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Output directory: {output_dir} (source: {"explicit" if explicit.strip() else "auto-detected"})\n')
    web_root = Path(project.workspace_root) / 'runtime' / environment.name / 'web'
    web_root.mkdir(parents=True, exist_ok=True)

    # --- Fix ownership BEFORE building ---
    # Gunicorn (root) writes files, celery (saasclaw) builds them.
    # Normalize ownership so build tools don't hit permission errors.
    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(Path(project.workspace_root) / 'runtime', log_file)

    # --- Framework-specific build env ---
    build_env = {}

    # Inject DB-stored env vars into build environment (Vite needs VITE_* at build time)
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        build_env[ev.key] = ev.value
    # Also inject into repo .env so Vite picks them up automatically
    if build_env:
        repo_env_for_build = dict(repo_env)
        for ev in EnvironmentVariable.objects.filter(environment=environment):
            repo_env_for_build[ev.key] = ev.value
        _write_text(repo_path / '.env', _serialize_env_file(repo_env_for_build))

    is_hugo = 'hugo' in (build_cmd or '').lower() or (repo_path / 'hugo.toml').exists()
    is_node = (repo_path / 'package.json').exists()

    if is_hugo:
        build_env['HUGO_CACHEDIR'] = '/tmp/hugo_cache'
        lock_file = repo_path / '.hugo_build.lock'
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                _run_command(f'rm -f {lock_file}', repo_path, log_file)
        logger.info('Hugo deploy: cleared lock, set HUGO_CACHEDIR')

    if is_node:
        build_env['npm_config_cache'] = '/tmp/npm_cache'
        # Detect Node version and prepend to PATH
        node_major = _detect_node_version(repo_path)
        if node_major:
            node_bin_dir = _node_binary_path(node_major)
            if node_bin_dir:
                import os as _os
                build_env['PATH'] = f"{node_bin_dir}:{_os.environ.get('PATH', '')}"
                with log_file.open('a', encoding='utf-8') as handle:
                    handle.write(f'Detected Node version: v{node_major} ({node_bin_dir})\n')
        node_modules = repo_path / 'node_modules'
        if node_modules.exists():
            _run_command(f'chown -R saasclaw:saasclaw {node_modules}', repo_path, log_file)
        logger.info('Node deploy: set npm_config_cache=/tmp/npm_cache')

    if environment.install_command:
        _run_command(environment.install_command, repo_path, log_file, env=build_env or None)
    if environment.build_command:
        _run_command(build_cmd, repo_path, log_file, env=build_env or None)

    output_path = repo_path / output_dir
    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Copying from {output_path} -> {web_root}\n')
    if output_path.exists():
        _publish_directory(output_path, web_root)
    else:
        # No build output dir — copy repo root (for plain HTML projects)
        with log_file.open('a', encoding='utf-8') as handle:
            handle.write(f'Warning: {output_path} does not exist, copying repo root instead\n')
        _publish_directory(repo_path, web_root)

    environment.web_root = str(web_root)
    environment.save(update_fields=['web_root', 'updated_at'])

    # Set up nginx (using sudo)
    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"
    _ensure_nginx_static(service_name, environment.domain, str(web_root), log_file=log_file)




"""Node SSR environment deployment — extracted from service.py.

Handles Next.js, Nuxt, and other Node.js server-side rendered projects.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

from .deploy_infra import (
    _load_env_file, _normalize_ownership, _run_command, _write_text,
    _slugify_system_name, _ensure_app_port, _ensure_systemd_service,
    _ensure_nginx_proxy, _ensure_postgres_database, _wait_for_http_healthcheck,
    _restart_service,
)

FNM_PATH = "/srv/saasclaw/.local/share/fnm"

if TYPE_CHECKING:
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.deployments.models import Deployment, Environment

logger = logging.getLogger(__name__)


def _detect_node_version(repo_path: Path) -> str | None:
    """Detect required Node version from repo files.
    Returns a version string like '18', '20', '22', or None for system default.
    """
    import re as _re

    # .nvmrc / .node-version
    for nv_file in ('.nvmrc', '.node-version'):
        f = repo_path / nv_file
        if f.exists():
            raw = f.read_text().strip().splitlines()[0].strip()
            # Handle formats like '18', '18.20.0', 'lts/hydrogen', 'v20'
            m = _re.match(r'v?(\d+)', raw)
            if m:
                return m.group(1)

    # package.json engines.node
    pkg_file = repo_path / 'package.json'
    if pkg_file.exists():
        try:
            import json as _json
            pkg = _json.loads(pkg_file.read_text())
            engines = pkg.get('engines', {})
            node_spec = engines.get('node', '')
            if node_spec:
                m = _re.search(r'(\d+)', node_spec)
                if m:
                    return m.group(1)
        except Exception:
            pass

    return None


def _node_binary_path(version: str) -> str:
    """Return the node binary directory for a given fnm version."""
    # Find the actual installed version dir
    import os as _os
    multishell = f'{FNM_PATH}/node-versions'
    if _os.path.isdir(multishell):
        for child in sorted(_os.listdir(multishell), reverse=True):
            if child.startswith(f'v{version}'):
                return f'{multishell}/{child}/installation/bin'
    return None



def _deploy_node_ssr_environment(project: Project, environment: Environment, deployment: Deployment, repo_path: Path, log_file: Path) -> None:
    """Deploy a Node SSR app (Next.js, Nuxt, etc.) to an environment."""
    _ensure_app_port(environment)
    import os as _os

    runtime_root = Path(project.workspace_root) / 'runtime' / environment.name
    runtime_root.mkdir(parents=True, exist_ok=True)

    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(runtime_root, log_file)

    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"
    port = environment.app_port or 0

    # Detect Node version
    build_env = {}
    node_major = _detect_node_version(repo_path)
    if node_major:
        node_bin_dir = _node_binary_path(node_major)
        if node_bin_dir:
            build_env['PATH'] = f"{node_bin_dir}:{_os.environ.get('PATH', '')}"
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write(f'Detected Node version: v{node_major} ({node_bin_dir})\n')
    build_env['npm_config_cache'] = '/tmp/npm_cache'

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Node SSR deploy for {project.slug}\n')
        handle.write(f'Port: {port}\n')
        handle.write(f'Service: {service_name}\n')

    # Install dependencies
    install_cmd = environment.install_command or 'npm install'
    _run_command(install_cmd, repo_path, log_file, env=build_env or None)

    # Build
    build_cmd = environment.build_command or 'npm run build'
    if build_cmd and build_cmd != 'none':
        _run_command(build_cmd, repo_path, log_file, env=build_env or None)

    # Provision PostgreSQL database
    env_suffix = f'_{environment.name}' if environment.name != 'preview' else ''
    default_db_name = f'saasclaw_{project.slug.replace("-", "_")}{env_suffix}'
    default_db_user = f'sc_{project.slug.replace("-", "_")}{env_suffix}'[:32]
    default_db_password = f'saasclaw-{project.slug}{env_suffix}-db'

    env_file = runtime_root / '.env'
    existing_env = _load_env_file(env_file)
    db_name = existing_env.get('POSTGRES_DB') or default_db_name
    db_user = existing_env.get('POSTGRES_USER') or default_db_user
    db_password = existing_env.get('POSTGRES_PASSWORD') or default_db_password
    db_host = existing_env.get('POSTGRES_HOST') or '127.0.0.1'
    db_port = existing_env.get('POSTGRES_PORT') or '5432'

    _ensure_postgres_database(db_name, db_user, db_password, log_file)

    # Standard DATABASE_URL for Node ORMs (Prisma, Drizzle, Knex, Sequelize, pg, etc.)
    database_url = f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
    env_lines = [
        f'PORT={port}',
        f'NODE_ENV=production',
        f'DATABASE_URL={database_url}',
        f'POSTGRES_DB={db_name}',
        f'POSTGRES_USER={db_user}',
        f'POSTGRES_PASSWORD={db_password}',
        f'POSTGRES_HOST={db_host}',
        f'POSTGRES_PORT={db_port}',
    ]
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        env_lines.append(f'{ev.key}={ev.value}')
    env_file.write_text('\n'.join(env_lines) + '\n', encoding='utf-8')
    _normalize_ownership(env_file, log_file)

    # Auto-run Prisma migrations if prisma/schema.prisma exists
    prisma_schema = repo_path / 'prisma' / 'schema.prisma'
    if prisma_schema.is_file():
        with log_file.open('a', encoding='utf-8') as h:
            h.write('Detected Prisma schema, running db push...\n')
        prisma_env = dict(build_env or {})
        for line in env_lines:
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                prisma_env[k] = v
        _run_command('npx prisma db push --skip-generate', repo_path, log_file, env=prisma_env)
        with log_file.open('a', encoding='utf-8') as h:
            h.write('Prisma db push complete.\n')

    # Get the node/npm binary paths
    if node_major and node_bin_dir:
        npm_bin = f"{node_bin_dir}/npm"
    else:
        npm_bin = '/usr/bin/npm'

    # Systemd service (always updated)
    _ensure_systemd_service(
        service_name=service_name,
        cwd=str(repo_path),
        env_file=str(env_file),
        exec_start=f'{npm_bin} start -- --port {port}',
        description=f'SaaSClaw Node SSR app for {service_name}',
    )

    # Nginx proxy (always updated, with WebSocket upgrade for Next.js HMR)
    _ensure_nginx_proxy(service_name, environment.domain, port, log_file=log_file, upgrade=True)

    # Start service
    _restart_service(service_name, log_file)

    # Healthcheck
    health_url = f'https://{environment.domain}/'
    _wait_for_http_healthcheck(health_url, log_file)



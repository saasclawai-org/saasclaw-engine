""".NET environment deployment — extracted from service.py.

Handles .NET/ASP.NET Core project detection, SDK installation, and deployment.
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
    _load_env_file, _serialize_env_file, _write_text, _normalize_ownership,
    _run_command, _slugify_system_name, _ensure_app_port,
    _ensure_systemd_service, _ensure_nginx_proxy, _ensure_nginx_spa_proxy,
    _ensure_postgres_database, _wait_for_http_healthcheck, _restart_service,
)

if TYPE_CHECKING:
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.deployments.models import Deployment, Environment

logger = logging.getLogger(__name__)


def _ensure_dotnet_sdk(log_file: Path) -> str:
    """Ensure .NET SDK is installed. Returns the dotnet binary path."""
    import shutil as _shutil
    dotnet = _shutil.which("dotnet")
    ef_tool = "/usr/local/bin/dotnet-ef"
    ef_env = {"DOTNET_ROOT": "/usr/local/share/dotnet"}
    if dotnet:
        with log_file.open('a', encoding='utf-8') as h:
            h.write(f'Using existing .NET SDK: {dotnet}\n')
            r = __import__('subprocess').run(['dotnet', '--version'], capture_output=True, text=True, timeout=15)
            h.write(f'.NET version: {r.stdout.strip()}\n')
        return dotnet
    # Install .NET 9 SDK (LTS)
    with log_file.open('a', encoding='utf-8') as h:
        h.write('Installing .NET 9 SDK...\n')
    install_script = Path('/tmp/dotnet-install.sh')
    _run_command('curl -sSL -o /tmp/dotnet-install.sh https://dot.net/v1/dotnet-install.sh', Path('/tmp'), log_file)
    _run_command('sudo bash /tmp/dotnet-install.sh --channel 9.0 --install-dir /usr/local/share/dotnet', Path('/tmp'), log_file)
    _run_command('sudo ln -sf /usr/local/share/dotnet/dotnet /usr/local/bin/dotnet', Path('/'), log_file)
    return '/usr/local/bin/dotnet'


def _detect_dotnet_entrypoint(repo_path: Path, project_slug: str) -> str:
    """Detect the DLL to run (e.g., App.dll, MyProject.dll)."""
    # Look for *.csproj to determine project name
    import glob as _glob
    csprojs = list(repo_path.glob('*.csproj'))
    if len(csprojs) == 1:
        project_name = csprojs[0].stem
    else:
        sln_files = list(repo_path.glob('*.sln'))
        if sln_files:
            # Try to find the main project (has Program.cs or is a web project)
            for csproj in repo_path.glob('**/*.csproj'):
                content = csproj.read_text(errors='replace')
                if 'WebApplication' in content or 'CreateBuilder' in content:
                    project_name = csproj.stem
                    break
            else:
                project_name = project_slug.replace('-', '_')
        else:
            project_name = project_slug.replace('-', '_')
    return f'{project_name}.dll'



def _reconcile_ef_migrations(repo_path: Path, db_conn: str, log_file: Path, env: dict | None = None) -> None:
    """Reconcile EF migration history with existing database tables.

    If the database has tables but __EFMigrationsHistory is empty, the tables
    were likely created by EnsureCreated() or direct SQL. In that case, we
    need to insert migration records for all existing migrations so EF knows
    they've already been applied, preventing "relation already exists" errors.
    """
    if not db_conn:
        return

    migrations_dir = repo_path / 'Migrations'
    if not migrations_dir.is_dir():
        return

    try:
        # Connect directly to the project's database using psycopg
        # (Django's default connection points to the SaaSClaw main DB, not
        # the project's DB)
        import psycopg
        conn = psycopg.connect(db_conn, autocommit=True)
        cur = conn.cursor()

        # Check if __EFMigrationsHistory has records
        cur.execute('SELECT COUNT(*) FROM "__EFMigrationsHistory"')
        count = cur.fetchone()[0]

        if count > 0:
            cur.close()
            conn.close()
            return

        # Check if any user tables exist
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name NOT IN ('__EFMigrationsHistory', '__EFMigrationsHistoryLock')
        """)
        table_count = cur.fetchone()[0]

        if table_count == 0:
            cur.close()
            conn.close()
            return

        # Tables exist but no migration history — seed migration records
        migrations = []
        for f in sorted(migrations_dir.glob('*.cs')):
            if 'Designer' in f.name or 'Snapshot' in f.name:
                continue
            migrations.append(f.name.replace('.cs', ''))

        # Determine EF version from snapshot
        ef_version = '9.0.0'
        snapshot = migrations_dir / 'AppDbContextModelSnapshot.cs'
        if snapshot.exists():
            import re
            content = snapshot.read_text(errors='ignore')
            match = re.search(r'ProductVersion.*?"([^"]+)"', content)
            if match:
                ef_version = match.group(1)

        for migration_id in migrations:
            log_file.write(f'Inserting migration record: {migration_id}\n')
            log_file.flush()
            cur.execute(
                'INSERT INTO "__EFMigrationsHistory" ("MigrationId", "ProductVersion") VALUES (%s, %s) ON CONFLICT DO NOTHING',
                (migration_id, ef_version)
            )

        log_file.write(f'Reconciled {len(migrations)} EF migration records\n')
        log_file.flush()
        cur.close()
        conn.close()

    except Exception as exc:
        log_file.write(f'WARNING: Could not reconcile EF migrations: {exc}\n')
        log_file.flush()


def _deploy_dotnet_environment(project: Project, environment: Environment, deployment: Deployment, repo_path: Path, log_file: Path) -> None:
    """Deploy a .NET app to an environment."""
    _ensure_app_port(environment)
    runtime_root = Path(project.workspace_root) / 'runtime' / environment.name
    runtime_root.mkdir(parents=True, exist_ok=True)

    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(runtime_root, log_file)

    env_file = runtime_root / '.env'
    existing_env = _load_env_file(env_file)
    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"
    publish_dir = runtime_root / 'publish'

    # Ensure .NET SDK
    dotnet = _ensure_dotnet_sdk(log_file)
    ef_tool = "/usr/local/bin/dotnet-ef"

    # Provision PostgreSQL database
    env_suffix = f'_{environment.name}' if environment.name != 'preview' else ''
    default_db_name = f'saasclaw_{project.slug.replace("-", "_")}{env_suffix}'
    default_db_user = f'sc_{project.slug.replace("-", "_")}{env_suffix}'[:32]
    default_db_password = f'saasclaw-{project.slug}{env_suffix}-db'

    db_name = existing_env.get('POSTGRES_DB') or default_db_name
    db_user = existing_env.get('POSTGRES_USER') or default_db_user
    db_password = existing_env.get('POSTGRES_PASSWORD') or default_db_password
    db_host = existing_env.get('POSTGRES_HOST') or '127.0.0.1'
    db_port = existing_env.get('POSTGRES_PORT') or '5432'

    _ensure_postgres_database(db_name, db_user, db_password, log_file)

    # Build both connection string formats
    npgsql_url = f'Host={db_host};Port={db_port};Database={db_name};Username={db_user};Password={db_password}'
    database_url = f'postgresql+psycopg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'

    # Build app settings / env vars
    # Also merge repo .env for JWT keys, API keys, etc.
    repo_env_file = repo_path / '.env'
    repo_env = _load_env_file(repo_env_file) if repo_env_file.exists() else {}
    env_values = dict(existing_env)
    env_values.update(repo_env)
    env_values.update({
        'ASPNETCORE_ENVIRONMENT': 'Development' if environment.name == 'preview' else 'Production',
        'ASPNETCORE_URLS': f'http://0.0.0.0:{environment.app_port}',
        'DATABASE_URL': database_url,
        'POSTGRES_DB': db_name,
        'POSTGRES_USER': db_user,
        'POSTGRES_PASSWORD': db_password,
        'POSTGRES_HOST': db_host,
        'POSTGRES_PORT': db_port,
        'ConnectionStrings__DefaultConnection': npgsql_url,
    })

    # Merge user-defined environment variables
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        env_values[ev.key] = ev.value

    _write_text(env_file, _serialize_env_file(env_values))

    # Detect entrypoint
    entrypoint_dll = _detect_dotnet_entrypoint(repo_path, project.slug)

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'.NET runtime root: {runtime_root}\n')
        handle.write(f'.NET env file: {env_file}\n')
        handle.write(f'.NET publish dir: {publish_dir}\n')
        handle.write(f'.NET entrypoint: {entrypoint_dll}\n')
        handle.write(f'.NET app port: {environment.app_port}\n')
        handle.write(f'.NET service name: {service_name}\n')

    # Build frontend before dotnet publish (so published output includes static assets)
    frontend_dir = repo_path / 'frontend'
    frontend_pkg = frontend_dir / 'package.json'
    vite_config = frontend_dir / 'vite.config.js'
    has_frontend = False
    vite_outputs_wwwroot = False
    if frontend_dir.is_dir() and frontend_pkg.is_file():
        if vite_config.is_file():
            vc_content = vite_config.read_text()
            if '../wwwroot' in vc_content or 'wwwroot' in vc_content:
                vite_outputs_wwwroot = True
        npm_cache = '/tmp/npm_cache'
        os.makedirs(npm_cache, exist_ok=True)
        _run_command(f'npm install --cache {npm_cache}', str(frontend_dir), log_file)
        if vite_outputs_wwwroot:
            _run_command(f'npx vite build', str(frontend_dir), log_file)
            has_frontend = True

    # Clean stale obj/Release to avoid dotnet publish stale asset errors
    import shutil
    obj_release = repo_path / 'obj' / 'Release'
    if obj_release.is_dir():
        shutil.rmtree(obj_release, ignore_errors=True)

    # Restore and publish
    _run_command(f'{dotnet} restore', repo_path, log_file)
    publish_dir.mkdir(parents=True, exist_ok=True)
    _run_command(f'{dotnet} publish -c Release -o {publish_dir}', repo_path, log_file)

    # Run EF Core migrations if Migrations directory exists or if no EnsureCreated seed
    migrations_dir = repo_path / 'Migrations'
    db_conn = env_values.get('ConnectionStrings__DefaultConnection', '')
    ef_env = {"DOTNET_ROOT": "/usr/local/share/dotnet"}
    if db_conn:
        ef_env["DOTNET_CONNECTION_STRING"] = db_conn
        ef_env["ConnectionStrings__DefaultConnection"] = db_conn

    if not migrations_dir.is_dir():
        # Generate initial migration
        try:
            _run_command(
                f'{ef_tool} migrations add InitialCreate --output-dir Migrations --context AppDbContext --project {repo_path}',
                repo_path, log_file, env=ef_env,
            )
            _run_command(f'sudo -u saasclaw git -c user.email="deploy@saasclaw.ai" -c user.name="deploy" add Migrations/ {migrations_dir}/*.cs', repo_path, log_file)
            _run_command(f'sudo -u saasclaw git -c user.email="deploy@saasclaw.ai" -c user.name="deploy" commit -m "auto: add EF migrations"', repo_path, log_file)
            _run_command(f'sudo GIT_SSH_COMMAND="ssh -i /home/nmoore/.ssh/id_ed25519_github_personal" -u saasclaw git push origin main', repo_path, log_file)
        except RuntimeError as e:
            log_file.write(f'WARNING: Could not generate initial EF migration: {e}\n')
            log_file.flush()
    else:
        # Check if there are pending model changes needing a new migration
        # Use a unique name to avoid collision with existing migrations
        import time
        migration_name = f"AutoMigrate{int(time.time())}"
        try:
            _run_command(
                f'{ef_tool} migrations add {migration_name} --output-dir Migrations --context AppDbContext --project {repo_path}',
                repo_path, log_file, env=ef_env,
            )
            _run_command(f'sudo -u saasclaw git -c user.email="deploy@saasclaw.ai" -c user.name="deploy" add Migrations/', repo_path, log_file)
            _run_command(f'sudo -u saasclaw git -c user.email="deploy@saasclaw.ai" -c user.name="deploy" commit -m "auto: update EF migrations"', repo_path, log_file)
            _run_command(f'sudo GIT_SSH_COMMAND="ssh -i /home/nmoore/.ssh/id_ed25519_github_personal" -u saasclaw git push origin main', repo_path, log_file)
        except RuntimeError:
            pass  # No model changes — expected on most deploys

    # Apply all pending migrations (run from repo_path where .csproj lives)
    #
    # Edge case: if tables already exist (e.g. from EnsureCreated) but
    # __EFMigrationsHistory is empty, we need to mark existing migrations
    # as already applied before running database update, otherwise EF tries
    # to re-create tables and fails with "relation already exists".
    _reconcile_ef_migrations(repo_path, db_conn, log_file, env=ef_env)

    _run_command(
        f'{ef_tool} database update --context AppDbContext --project {repo_path}',
        publish_dir, log_file, env=ef_env,
    )

    # Normalize ownership of published output
    _normalize_ownership(publish_dir, log_file)

    # Write env file next to published dll so systemd can pick it up
    _write_text(publish_dir / '.env', _serialize_env_file(env_values))

    # Determine nginx config based on frontend setup
    frontend_web_root = None
    static_root = publish_dir / 'wwwroot'
    if has_frontend and not vite_outputs_wwwroot:
        # Vite outputs to frontend/dist — nginx serves static files
        frontend_dist = frontend_dir / 'dist'
        if frontend_dist.is_dir():
            frontend_web_root = str(frontend_dist)

    # Systemd service (always updated)
    _ensure_systemd_service(
        service_name=service_name,
        cwd=str(publish_dir),
        env_file=str(publish_dir / '.env'),
        exec_start=f'/usr/local/bin/dotnet {entrypoint_dll} --urls http://0.0.0.0:{environment.app_port}',
        description=f'SaaSClaw .NET app for {service_name}',
    )

    # Nginx proxy (SPA via nginx, or standard if dotnet serves static files)
    if has_frontend and frontend_web_root:
        _ensure_nginx_spa_proxy(service_name, environment.domain, environment.app_port, frontend_web_root, str(static_root) if static_root.is_dir() else None, log_file)
    else:
        _ensure_nginx_proxy(service_name, environment.domain, environment.app_port, log_file=log_file)

    # Start service
    _restart_service(service_name, log_file)

    # Healthcheck
    healthcheck_path = '/health'
    health_url = f'https://{environment.domain}{healthcheck_path}'
    _wait_for_http_healthcheck(health_url, log_file)

    environment.deploy_path = str(runtime_root)
    environment.save(update_fields=['deploy_path', 'updated_at'])

"""Django/Python environment deployment — extracted from service.py.

Detects Python/WSGI entrypoints, configures venv, migrates, and deploys
Django/Flask/FastAPI projects.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

from django.conf import settings

from .deploy_infra import (
    _load_env_file, _serialize_env_file, _write_text, _normalize_ownership,
    _run_command, _slugify_system_name, _ensure_app_port,
    _ensure_postgres_database, _wait_for_http_healthcheck,
    _ensure_systemd_service, _ensure_nginx_spa_proxy,
    _restart_service, _ensure_django_admin_user,
)

logger = logging.getLogger(__name__)


def _detect_wsgi_entrypoint(repo_path: Path, slug: str) -> str:
    """Auto-detect the WSGI entrypoint from the repo structure."""
    # Check config/wsgi.py first (most common for Django projects)
    if (repo_path / 'config' / 'wsgi.py').exists():
        return 'config.wsgi:application'
    # Check for a package with wsgi.py
    for item in repo_path.iterdir():
        if item.is_dir() and (item / 'wsgi.py').exists() and (item / '__init__.py').exists():
            return f'{item.name}.wsgi:application'
    # Fallback to slug-based
    return slug.replace('-', '_') + '.wsgi:application'


def _detect_python_entrypoint(repo_path: Path) -> str:
    """Auto-detect ASGI/WSGI entrypoint for FastAPI/Flask apps."""
    # Check app.py for FastAPI/Flask app instance
    app_file = repo_path / 'app.py'
    if app_file.exists():
        content = app_file.read_text(encoding='utf-8', errors='replace')
        if 'FastAPI' in content or 'fastapi' in content:
            return 'app:app'
        if 'Flask' in content or 'flask' in content:
            return 'app:app'
    main_file = repo_path / 'main.py'
    if main_file.exists():
        return 'main:app'
    return 'app:app'


# System-wide Python versions (via deadsnakes PPA)
# Maps major.minor -> system binary path
PYTHON_BINARIES = {
    '3.11': '/usr/bin/python3.11',
    '3.12': '/usr/bin/python3.12',
    '3.13': '/usr/bin/python3.13',
    '3.14': '/usr/bin/python3',  # System default
}


def _available_python_versions() -> list[str]:
    """List available Python versions on the system."""
    versions = []
    for ver, binary in PYTHON_BINARIES.items():
        if Path(binary).exists():
            versions.append(ver)
    return versions


def _detect_python_version(repo_path: Path) -> str:
    """Detect required Python version from repo files.
    Returns a major.minor string like '3.12', or '3.14' for system default.
    """
    import re as _re
    available = _available_python_versions()
    wanted = None

    # .python-version (pyenv standard)
    pv_file = repo_path / '.python-version'
    if pv_file.exists():
        raw = pv_file.read_text().strip().splitlines()[0].strip()
        if raw:
            m = _re.match(r'(\d+\.\d+)', raw)
            if m:
                wanted = m.group(1)

    # runtime.txt (Heroku/Replit style)
    if not wanted:
        rt_file = repo_path / 'runtime.txt'
        if rt_file.exists():
            raw = rt_file.read_text().strip()
            m = _re.match(r'python-(\d+\.\d+)', raw)
            if m:
                wanted = m.group(1)

    # Pipfile
    if not wanted:
        pf_file = repo_path / 'Pipfile'
        if pf_file.exists():
            content = pf_file.read_text(encoding='utf-8', errors='replace')
            m = _re.search(r'python_version\s*=\s*["\'](\d+\.\d+)', content)
            if m:
                wanted = m.group(1)

    # setup.py / pyproject.toml requires-python
    if not wanted:
        for cfg in ('pyproject.toml', 'setup.py', 'setup.cfg'):
            f = repo_path / cfg
            if not f.exists():
                continue
            content = f.read_text(encoding='utf-8', errors='replace')
            m = _re.search(r'(?:requires-python|python_requires)\s*[=<>\"\'\s]*(\d+\.\d+)', content)
            if m:
                wanted = m.group(1)
                break

    if not wanted or wanted not in available:
        return '3.14'  # System default

    return wanted


def _python_binary_for_version(version: str) -> str:
    """Return the python binary path for a given version string."""
    return PYTHON_BINARIES.get(version, '/usr/bin/python3')


# fnm root for Node version management (saasclaw user)
FNM_PATH = '/srv/saasclaw/.local/share/fnm'


def _configure_django_runtime_service(
    helper_script: Path, service_name: str, repo_path: Path, env_file: Path,
    entrypoint: str, port: int, domain: str, static_root: Path, log_file: Path,
) -> None:
    """Configure and restart the systemd service for a Django app."""
    service_file = Path(f'/etc/systemd/system/{service_name}.service')
    if service_file.exists():
        # Service already configured, just restart
        with log_file.open('a', encoding='utf-8') as handle:
            handle.write(f'Service {service_name} already exists, restarting\n')
    elif helper_script.exists():
        # Detect if this is an ASGI app (FastAPI) needing uvicorn worker
        worker_class = ''
        app_py = repo_path / 'app.py'
        if app_py.exists():
            content = app_py.read_text(encoding='utf-8', errors='replace')
            if 'FastAPI' in content or 'fastapi' in content:
                worker_class = '-k uvicorn.workers.UvicornWorker'
        env = dict(__import__('os').environ)
        env['GUNICORN_WORKER_CLASS'] = worker_class
        _run_command(
            f'sudo GUNICORN_WORKER_CLASS="{worker_class}" {helper_script} {service_name} {repo_path} {env_file} '
            f'{entrypoint} {port} {domain} {static_root}',
            repo_path, log_file, env=env,
        )
    else:
        with log_file.open('a', encoding='utf-8') as handle:
            handle.write(f'Cannot configure service: {service_name} not found and {helper_script} missing\n')
        return
    _run_command(f'sudo -n /bin/systemctl restart {service_name} 2>&1 || systemctl restart {service_name}', repo_path, log_file)


def _deploy_django_environment(project: Project, environment: Environment, deployment: Deployment, repo_path: Path, log_file: Path) -> None:
    """Deploy a Django app to an environment."""
    _ensure_app_port(environment)
    runtime_root = Path(project.workspace_root) / 'runtime' / environment.name
    runtime_root.mkdir(parents=True, exist_ok=True)

    # --- Fix ownership BEFORE building ---
    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(runtime_root, log_file)

    env_file = runtime_root / '.env'
    existing_env = _load_env_file(env_file)
    venv_path = runtime_root / '.venv'
    static_root = runtime_root / 'staticfiles'
    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"
    helper_script = Path('/usr/local/bin/configure-django-preview-runtime')

    env_suffix = f'_{environment.name}' if environment.name != 'preview' else ''
    default_db_name = f'saasclaw_{project.slug.replace("-", "_")}{env_suffix}'
    default_db_user = f'sc_{project.slug.replace("-", "_")}{env_suffix}'[:32]
    default_db_password = f'saasclaw-{project.slug}{env_suffix}-db'

    db_name = existing_env.get('POSTGRES_DB') or default_db_name
    db_user = existing_env.get('POSTGRES_USER') or default_db_user
    db_password = existing_env.get('POSTGRES_PASSWORD') or default_db_password
    django_secret_key = existing_env.get('DJANGO_SECRET_KEY') or f'saasclaw-{project.slug}-{environment.name}'
    # Generate a random admin password on first deploy; reuse on redeploys
    import secrets as _secrets
    admin_password = existing_env.get('DJANGO_SUPERUSER_PASSWORD') or _secrets.token_urlsafe(12)
    admin_username = existing_env.get('DJANGO_SUPERUSER_USERNAME') or 'admin'
    django_settings_module = existing_env.get('DJANGO_SETTINGS_MODULE') or (
        environment.python_entrypoint.split(':')[0].rsplit('.', 1)[0] + '.settings'
        if environment.python_entrypoint else None
    )
    if not django_settings_module:
        # Auto-detect from the repo's manage.py or wsgi.py
        import re as _re
        for check_file in ['manage.py', 'wsgi.py', 'config/asgi.py', 'config/wsgi.py']:
            check_path = repo_path / check_file
            if check_path.exists():
                content = check_path.read_text(errors='replace')
                m = _re.search(r"DJANGO_SETTINGS_MODULE['\"]?,\s*['\"]([^'\"]+)", content)
                if m:
                    django_settings_module = m.group(1)
                    break
        if not django_settings_module:
            django_settings_module = f'{project.slug.replace("-", "_")}.settings'

    env_values = dict(existing_env)
    env_values.update({
        'DJANGO_SECRET_KEY': django_secret_key,
        'DJANGO_SETTINGS_MODULE': django_settings_module,
        'DJANGO_DEBUG': 'true' if environment.name == 'preview' else 'false',
        'DJANGO_ALLOWED_HOSTS': environment.domain + ',127.0.0.1',
        'DJANGO_CSRF_TRUSTED_ORIGINS': f'https://{environment.domain}',
        'ALLOWED_HOSTS': environment.domain,
        'CSRF_TRUSTED_ORIGINS': f'https://{environment.domain}',
        'DJANGO_STATIC_ROOT': str(static_root),
        'DJANGO_SUPERUSER_USERNAME': admin_username,
        'DJANGO_SUPERUSER_PASSWORD': admin_password,
        'DJANGO_SUPERUSER_EMAIL': f'{admin_username}@{project.slug}.saasclaw.ai',
        'POSTGRES_DB': db_name,
        'POSTGRES_USER': db_user,
        'POSTGRES_PASSWORD': db_password,
        'POSTGRES_HOST': existing_env.get('POSTGRES_HOST') or '127.0.0.1',
        'POSTGRES_PORT': existing_env.get('POSTGRES_PORT') or '5432',
        'DATABASE_URL': f'postgresql+psycopg://{db_user}:{db_password}@{existing_env.get("POSTGRES_HOST") or "127.0.0.1"}:{existing_env.get("POSTGRES_PORT") or "5432"}/{db_name}',
    })
    # Merge user-defined environment variables (override defaults)
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        env_values[ev.key] = ev.value
    env_text = _serialize_env_file(env_values)
    _write_text(env_file, env_text)
    _ensure_postgres_database(db_name, db_user, db_password, log_file)

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Django runtime root: {runtime_root}\n')
        handle.write(f'Django env file: {env_file}\n')
        handle.write(f'Django venv path: {venv_path}\n')
        handle.write(f'Django app port: {environment.app_port}\n')
        handle.write(f'Django service name: {service_name}\n')

    # Detect Python version from repo
    py_version = _detect_python_version(repo_path)
    py_bin = _python_binary_for_version(py_version)
    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Detected Python version: {py_version} ({py_bin})\n')

    # Install dependencies
    if not (venv_path / 'bin' / 'python').exists():
        _run_command(f'{py_bin} -m venv {venv_path}', repo_path, log_file)
    _run_command(f'{venv_path}/bin/pip install -r requirements.txt', repo_path, log_file)

    # Django-specific steps (skip for FastAPI/Flask without manage.py)
    has_manage = (repo_path / 'manage.py').exists()
    has_frontend = False
    frontend_web_root = None
    if has_manage:
        # React frontend build (e.g. react-django template)
        frontend_dir = repo_path / 'frontend'
        frontend_pkg = frontend_dir / 'package.json'
        if frontend_dir.is_dir() and frontend_pkg.is_file():
            import shutil as _shutil
            npm_cache = '/tmp/npm_cache'
            os.makedirs(npm_cache, exist_ok=True)
            _run_command(f'npm install --cache {npm_cache}', str(frontend_dir), log_file)
            _run_command(f'npx vite build --outDir dist', str(frontend_dir), log_file)
            frontend_dist = frontend_dir / 'dist'
            if frontend_dist.is_dir():
                frontend_web_root = str(frontend_dist)
                has_frontend = True

        # Migrate (auto-create migrations for new apps)
        _run_command(f'bash -lc "set -a && source {env_file} && {venv_path}/bin/python manage.py makemigrations --noinput 2>/dev/null || true"', repo_path, log_file)
        # Use smart migrate that detects rewritten 0001_initial (wizard may modify initial migration)
        _run_command(f'django-force-migrate {repo_path} {env_file} {venv_path}', repo_path, log_file)

        # Admin user
        _ensure_django_admin_user(repo_path, env_file, venv_path, deployment, log_file)

        # Collect static
        static_root.mkdir(parents=True, exist_ok=True)
        _run_command(f'bash -lc "set -a && source {env_file} && {venv_path}/bin/python manage.py collectstatic --noinput"', repo_path, log_file)
        entrypoint = environment.python_entrypoint or _detect_wsgi_entrypoint(repo_path, project.slug)
    else:
        # FastAPI/Flask: detect ASGI/WSGI app
        entrypoint = environment.python_entrypoint or _detect_python_entrypoint(repo_path)
        static_root.mkdir(parents=True, exist_ok=True)

    # Configure and restart service
    _configure_django_runtime_service(
        helper_script=helper_script,
        service_name=service_name,
        repo_path=repo_path,
        env_file=env_file,
        entrypoint=entrypoint,
        port=environment.app_port,
        domain=environment.domain,
        static_root=static_root,
        log_file=log_file,
    )

    # Healthcheck
    # Write SPA nginx BEFORE healthcheck so it routes correctly
    if has_frontend and frontend_web_root:
        _ensure_nginx_spa_proxy(service_name, environment.domain, environment.app_port, frontend_web_root, static_root, log_file)
    healthcheck_path = '/api/health/' if has_frontend else (environment.healthcheck_path or '/health/')
    health_url = f'https://{environment.domain}{healthcheck_path}'
    _wait_for_http_healthcheck(health_url, log_file)

    environment.web_root = str(static_root)
    environment.deploy_path = str(runtime_root)
    environment.save(update_fields=['web_root', 'deploy_path', 'updated_at'])



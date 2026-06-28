import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from django.utils import timezone as dj_timezone

from saasclaw_engine.integrations.github import clone_or_update_repo
from saasclaw_engine.projects.models import Project
from saasclaw_engine.deployments.models import Deployment, Environment

logger = logging.getLogger(__name__)


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
    return values


def _serialize_env_file(values: dict[str, str]) -> str:
    lines = []
    for key, value in sorted(values.items()):
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _normalize_ownership(path: Path, log_file: Path = None) -> None:
    """Ensure all files in a path are owned by saasclaw:saasclaw.

    Gunicorn runs as root, celery runs as saasclaw. When the agent (root)
    writes files, celery can't read/build them. This normalizes ownership
    before any build step.
    """
    try:
        import pwd as _pwd
        saasclaw_uid = _pwd.getpwnam('saasclaw').pw_uid
        saasclaw_gid = _pwd.getpwnam('saasclaw').pw_gid
        subprocess.run(
            f'chown -R {saasclaw_uid}:{saasclaw_gid} {path}',
            shell=True, capture_output=True, text=True, timeout=120
        )
        if log_file:
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write(f'[normalized ownership: {path}]\n')
    except Exception as e:
        if log_file:
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write(f'[ownership normalize failed: {e}]\n')


def _run_command(command: str, cwd: Path, log_file: Path, env: dict = None) -> None:
    """Run a shell command, optionally with extra env vars."""
    import os as _os
    full_env = _os.environ.copy()
    if env:
        full_env.update(env)
    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'$ {command}\n')
        result = subprocess.run(
            command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=300,
            env=full_env,
        )
        if result.stdout:
            handle.write(result.stdout)
        if result.stderr:
            handle.write(result.stderr)
        handle.write(f'[exit {result.returncode}]\n')
        if result.returncode != 0:
            raise RuntimeError(f'Command failed (exit {result.returncode}): {command}')


def _run_logged_subprocess(args: list[str], cwd: Path, log_file: Path) -> None:
    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'$ {" ".join(args)}\n')
        result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=300)
        if result.stdout:
            handle.write(result.stdout)
        if result.stderr:
            handle.write(result.stderr)
        handle.write(f'[exit {result.returncode}]\n')
        if result.returncode != 0:
            raise RuntimeError(f'Command failed (exit {result.returncode}): {" ".join(args)}')


def _tail_text(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ''
    text = path.read_text(encoding='utf-8', errors='replace')
    if len(text) <= limit:
        return text
    return '...' + text[-limit:]


def _repo_commit_sha(repo_path: Path, ref: str = 'HEAD') -> str:
    result = subprocess.run(
        ['git', 'rev-parse', ref], cwd=str(repo_path), capture_output=True, text=True,
    )
    return result.stdout.strip()


def _remote_repo_url(repo_path: Path) -> str:
    result = subprocess.run(
        ['git', 'remote', 'get-url', 'origin'], cwd=str(repo_path), capture_output=True, text=True,
    )
    return result.stdout.strip()


def _normalize_repo_url(url: str) -> str:
    """Normalize repo URL for comparison (strip protocol, tokens, .git suffix)."""
    if not url:
        return ''
    url = url.strip()
    # Convert SSH to HTTPS form for comparison
    if url.startswith('git@github.com:'):
        url = 'https://github.com/' + url.split(':', 1)[1]
    # Strip embedded tokens: https://x-access-token:TOKEN@github.com/...
    import re
    url = re.sub(r'https?://[^@]+@', 'https://', url)
    return url.rstrip('/').removesuffix('.git')


def _assert_repo_binding(project: Project, repo_path: Path) -> None:
    """Verify the repo's remote matches the project's configured repo URL."""
    if not project.repo_url:
        return
    actual = _normalize_repo_url(_remote_repo_url(repo_path))
    expected = _normalize_repo_url(project.repo_url)
    if actual and expected and actual != expected:
        raise RuntimeError(f'Repo remote drift: expected {expected}, found {actual}')


def _refresh_repo_checkout_for_deploy(project: Project, repo_path: Path, log_file: Path) -> None:
    """Clone or pull the project repo for deploy."""
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    if not (repo_path / '.git').exists():
        clone_or_update_repo(project, target_path=repo_path)
    else:
        _assert_repo_binding(project, repo_path)
        branch = project.repo_default_branch or 'main'
        # Use GitHub App token for auth if this is a GitHub repo
        is_github = project.repo_provider == 'github' or 'github.com' in (project.repo_url or '')
        if is_github and project.repo_owner and project.repo_name:
            try:
                from saasclaw_engine.integrations.models import GitHubInstallation
                from saasclaw_engine.integrations.github import create_installation_access_token, _git_auth_args
                inst = GitHubInstallation.objects.filter(
                    account_name=project.repo_owner
                ).first() or GitHubInstallation.objects.first()
                if inst:
                    token = create_installation_access_token(inst.installation_id)
                    authed_url = f'https://x-access-token:{token}@github.com/{project.repo_owner}/{project.repo_name}.git'
                    _run_command(f'git remote set-url origin {authed_url}', repo_path, log_file)
            except Exception:
                pass
        elif is_github:
            logger.warning('GitHub repo missing owner/repo for project %s, converting to SSH', project.slug)

        # If remote uses expired token, convert to SSH
        try:
            import subprocess as _sp2
            url_result = _sp2.run(['git', 'remote', 'get-url', 'origin'], cwd=str(repo_path), capture_output=True, text=True, timeout=5)
            if 'x-access-token' in url_result.stdout:
                m = re.search(r'github\.com[:/](.+?)(?:\.git)?$', url_result.stdout.strip())
                if m:
                    ssh_url = f'git@github.com:{m.group(1)}.git'
                    _run_command(f'git remote set-url origin {ssh_url}', repo_path, log_file)
        except Exception:
            pass

        # Clean __pycache__ to avoid permission issues on git reset
        _run_command('find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true', repo_path, log_file)

        git_ssh = 'GIT_SSH_COMMAND="ssh -i /srv/saasclaw/.ssh/id_ed25519_deploy -o StrictHostKeyChecking=no" '
        _run_command(f'{git_ssh}git fetch origin', repo_path, log_file)
        _run_command(f'{git_ssh}git checkout {branch}', repo_path, log_file)
        _run_command(f'{git_ssh}git reset --hard origin/{branch}', repo_path, log_file)


def _slugify_system_name(value: str) -> str:
    return re.sub(r'[^a-z0-9-]', '-', value.lower()).strip('-') or 'app'


def _ensure_app_port(environment) -> int:
    """Assign a free port to the environment if not already set."""
    if environment.app_port:
        return environment.app_port
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    environment.app_port = port
    environment.save(update_fields=['app_port', 'updated_at'])
    return port


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


def _ensure_postgres_database(db_name: str, db_user: str, db_password: str, log_file: Path) -> None:
    """Create Postgres role and database if they don't exist (via psycopg3)."""
    from psycopg import sql as _sql
    import psycopg as _psy
    admin_dsn = "host=127.0.0.1 dbname=postgres user=saasclaw_admin password=saasclaw_admin_super_2024"
    try:
        with _psy.connect(admin_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (db_user,))
                if not cur.fetchone():
                    cur.execute(
                        _sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(
                            _sql.Identifier(db_user),
                            _sql.Literal(db_password),
                        )
                    )
                    with log_file.open('a') as h:
                        h.write(f'[created role: {db_user}]\n')
                else:
                    with log_file.open('a') as h:
                        h.write(f'[role exists: {db_user}]\n')
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                if not cur.fetchone():
                    cur.execute(
                        _sql.SQL('CREATE DATABASE {} OWNER {}').format(
                            _sql.Identifier(db_name),
                            _sql.Identifier(db_user),
                        )
                    )
                    with log_file.open('a') as h:
                        h.write(f'[created database: {db_name}]\n')
                else:
                    with log_file.open('a') as h:
                        h.write(f'[database exists: {db_name}]\n')
    except Exception as exc:
        with log_file.open('a') as h:
            h.write(f'[postgres error: {exc}]\n')
        raise


def _ensure_django_admin_user(repo_path: Path, env_file: Path, venv_path: Path, deployment: Deployment, log_file: Path) -> None:
    """Ensure a Django superuser exists with the password from env.

    Creates the user if missing, then always sets the password so it
    stays in sync with DJANGO_SUPERUSER_PASSWORD across redeploys.
    """
    # Step 1: create the user if it doesn't exist
    create_cmd = (
        f'bash -lc "set -a && source {env_file} && '
        f'DJANGO_SUPERUSER_USERNAME=${{DJANGO_SUPERUSER_USERNAME:-admin}} '
        f'DJANGO_SUPERUSER_EMAIL=${{DJANGO_SUPERUSER_EMAIL:-admin@example.com}} '
        f'DJANGO_SUPERUSER_PASSWORD=${{DJANGO_SUPERUSER_PASSWORD:-admin}} '
        f'{venv_path}/bin/python manage.py createsuperuser --noinput 2>&1 || true"'
    )
    _run_command(create_cmd, repo_path, log_file)

    # Step 2: force-set the password (survives redeploys, updates if changed)
    set_pw_script = repo_path / '.saasclaw_set_pw.py'
    set_pw_script.write_text(
        'import os, django\n'
        'os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings"))\n'
        'django.setup()\n'
        'from django.contrib.auth import get_user_model\n'
        'U = get_user_model()\n'
        'username = os.environ["DJANGO_SUPERUSER_USERNAME"]\n'
        'password = os.environ["DJANGO_SUPERUSER_PASSWORD"]\n'
        'u = U.objects.filter(username=username).first()\n'
        'if u:\n'
        '    u.set_password(password)\n'
        '    u.save()\n'
    )
    set_pw_cmd = (
        f'bash -lc "set -a && source {env_file} && '
        f'{venv_path}/bin/python {set_pw_script} 2>&1 || true"'
    )
    _run_command(set_pw_cmd, repo_path, log_file)
    # Clean up the temp script
    try:
        set_pw_script.unlink(missing_ok=True)
    except Exception:
        pass


def _wait_for_http_healthcheck(url: str, log_file: Path, attempts: int = 20, delay_seconds: float = 2.0) -> None:
    """Poll a URL until it returns 200 or attempts run out. Uses progressive backoff."""
    import requests as http_requests
    import time as time_module
    for attempt in range(1, attempts + 1):
        try:
            response = http_requests.get(url, timeout=10, allow_redirects=True)
            if response.status_code < 400:
                with log_file.open('a', encoding='utf-8') as handle:
                    handle.write(f'Healthcheck OK ({response.status_code}) after {attempt} attempt(s)\n')
                return
        except Exception as exc:
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write(f'Healthcheck attempt {attempt}/{attempts}: {exc}\n')
        # Progressive backoff: 2s, 2s, 3s, 3s, 4s, 4s, ... up to 5s
        backoff = min(delay_seconds + (attempt - 1) // 2, 5.0)
        time_module.sleep(backoff)
    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Healthcheck gave up after {attempts} attempts\n')


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
        'DJANGO_ALLOWED_HOSTS': environment.domain,
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
    if has_manage:
        # Migrate
        _run_command(f'bash -lc "set -a && source {env_file} && {venv_path}/bin/python manage.py migrate"', repo_path, log_file)

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
    health_url = f'https://{environment.domain}{environment.healthcheck_path or "/health/"}'
    _wait_for_http_healthcheck(health_url, log_file)

    environment.web_root = str(static_root)
    environment.deploy_path = str(runtime_root)
    environment.save(update_fields=['web_root', 'deploy_path', 'updated_at'])


def _publish_directory(source: Path, destination: Path) -> None:
    """Copy built static files to their destination."""
    # Clear destination first to avoid permission errors on existing files
    # owned by a different user from a previous deploy
    if destination.exists():
        import shutil as _sh
        for child in destination.iterdir():
            if child.is_dir():
                _sh.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except Exception:
                    pass
    destination.mkdir(parents=True, exist_ok=True)
    SKIP = {'.git', 'node_modules', '.env'}
    for child in source.iterdir():
        if child.name in SKIP:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)



def _pick_ssl_certs(domain):
    """Return (cert_path, key_path) for the given domain."""
    if '.preview.' in domain:
        base = 'preview.saasclaw.ai'
    else:
        base = 'saasclaw.ai'
    return (
        '/etc/letsencrypt/live/' + base + '/fullchain.pem',
        '/etc/letsencrypt/live/' + base + '/privkey.pem',
    )


def _ensure_systemd_service(service_name, cwd, env_file, exec_start,
                            user='saasclaw', description=''):
    """Write (or overwrite) a systemd unit file and reload daemon."""
    if not description:
        description = 'SaaSClaw service for ' + service_name
    unit_lines = [
        '[Unit]',
        'Description=' + description,
        'After=network.target',
        '',
        '[Service]',
        'Type=simple',
        'WorkingDirectory=' + cwd,
    ]
    if env_file:
        unit_lines.append('EnvironmentFile=' + env_file)
    unit_lines.extend([
        'ExecStart=' + exec_start,
        'Restart=always',
        'RestartSec=3',
        'User=' + user,
        '',
        '[Install]',
        'WantedBy=multi-user.target',
        '',
    ])
    unit = '\n'.join(unit_lines)
    service_file = Path('/etc/systemd/system/' + service_name + '.service')
    subprocess.run(
        ['sudo', 'tee', str(service_file)],
        input=unit.encode(), capture_output=True, timeout=10,
    )
    subprocess.run(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, timeout=10)
    subprocess.run(['sudo', 'systemctl', 'enable', service_name], capture_output=True, timeout=10)




def _write_and_validate_nginx(site_name, nginx_content, log_file=None):
    """Write nginx config, validate with nginx -t, and reload.
    If validation fails, roll back the config so we never leave nginx broken.
    Returns True on success, False on failure.
    """
    import logging
    _logger = logging.getLogger(__name__)
    site_file = Path('/etc/nginx/sites-available/' + site_name)
    site_enabled = Path('/etc/nginx/sites-enabled/' + site_name)
    
    # Write config
    result = subprocess.run(
        ['sudo', 'tee', str(site_file)],
        input=nginx_content.encode(), capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        msg = f"Failed to write nginx config {site_name}: {result.stderr.decode()[:200]}"
        _logger.error(msg)
        if log_file:
            with log_file.open('a', encoding='utf-8') as h:
                h.write(f"NGINX ERROR: {msg}\n")
        return False
    
    result = subprocess.run(
        ['sudo', 'ln', '-sfn', str(site_file), str(site_enabled)],
        capture_output=True, timeout=10,
    )
    
    # Validate before reload
    result = subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, timeout=10)
    if result.returncode != 0:
        error = result.stderr.decode()[:500]
        msg = f"nginx config validation FAILED for {site_name}: {error}"
        _logger.error(msg)
        if log_file:
            with log_file.open('a', encoding='utf-8') as h:
                h.write(f"NGINX ERROR: {msg}\n")
        # Roll back: remove the broken config
        subprocess.run(['sudo', 'rm', '-f', str(site_enabled)], capture_output=True, timeout=5)
        subprocess.run(['sudo', 'rm', '-f', str(site_file)], capture_output=True, timeout=5)
        # Try to reload the remaining good configs
        subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, timeout=10)
        subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)
        return False
    
    # Good — reload
    result = subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)
    if result.returncode != 0:
        msg = f"nginx reload failed after writing {site_name}: {result.stderr.decode()[:200]}"
        _logger.error(msg)
        if log_file:
            with log_file.open('a', encoding='utf-8') as h:
                h.write(f"NGINX ERROR: {msg}\n")
        return False
    
    return True

def _ensure_nginx_proxy(service_name, domain, port, log_file=None, upgrade=False):
    """Write (or overwrite) an nginx reverse-proxy site config and reload."""
    ssl_cert, ssl_key = _pick_ssl_certs(domain)
    upgrade_lines = [
        '        proxy_http_version 1.1;',
        '        proxy_set_header Upgrade $http_upgrade;',
        '        proxy_set_header Connection "upgrade";',
    ] if upgrade else []
    nginx_lines = [
        'server {',
        '    listen 80;',
        '    listen [::]:80;',
        '    server_name ' + domain + ';',
        '    return 301 https://$host$request_uri;',
        '}',
        '',
        'server {',
        '    listen 443 ssl;',
        '    listen [::]:44 ssl;',
        '    server_name ' + domain + ';',
        '',
        '    ssl_certificate ' + ssl_cert + ';',
        '    ssl_certificate_key ' + ssl_key + ';',
        '    include /etc/letsencrypt/options-ssl-nginx.conf;',
        '    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;',
        '',
        '    client_max_body_size 25m;',
        '',
        '    location / {',
        '        proxy_pass http://127.0.0.1:' + str(port) + ';',
        '        proxy_set_header Host $host;',
        '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        '        proxy_set_header X-Forwarded-Proto $scheme;',
        '        proxy_redirect off;',
    ]
    nginx_lines.extend(upgrade_lines)
    nginx_lines.extend([
        '    }',
        '}',
        '',
    ])
    nginx_content = '\n'.join(nginx_lines)
    nginx_content = '\n'.join(nginx_lines)
    if port <= 0:
        raise RuntimeError(f'Invalid nginx proxy port {port} for {service_name} — refusing to write config')
    if not _write_and_validate_nginx(service_name, nginx_content, log_file=log_file):
        raise RuntimeError(f'Failed to write/validate nginx config for {service_name}')


def _ensure_nginx_static(service_name, domain, web_root, log_file=None):
    """Write (or overwrite) an nginx static-file site config and reload."""
    ssl_cert, ssl_key = _pick_ssl_certs(domain)
    nginx_content = '\n'.join([
        'server {',
        '    listen 80;',
        '    listen [::]:80;',
        '    server_name ' + domain + ';',
        '    return 301 https://$host$request_uri;',
        '}',
        '',
        'server {',
        '    listen 443 ssl;',
        '    listen [::]:44 ssl;',
        '    server_name ' + domain + ';',
        '',
        '    ssl_certificate ' + ssl_cert + ';',
        '    ssl_certificate_key ' + ssl_key + ';',
        '    include /etc/letsencrypt/options-ssl-nginx.conf;',
        '    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;',
        '',
        '    client_max_body_size 25m;',
        '',
        '    root ' + web_root + ';',
        '    index index.html;',
        '',
        '    location / {',
        '        try_files $uri $uri/ /index.html;',
        '    }',
        '}',
        '',
    ])
    if not _write_and_validate_nginx(service_name, nginx_content, log_file=log_file):
        raise RuntimeError(f'Failed to write/validate nginx config for {service_name}')


def _restart_service(service_name, log_file=None):
    """Restart a systemd service."""
    subprocess.run(
        ['sudo', '-n', '/bin/systemctl', 'restart', service_name],
        capture_output=True, timeout=30,
    )



def _deploy_static_environment(project: Project, environment: Environment, deployment: Deployment, repo_path: Path, log_file: Path) -> None:
    """Deploy a static site to an environment."""
    build_cmd = environment.build_command or 'echo "No build step"'
    output_dir = environment.output_directory or 'dist'
    web_root = Path(project.workspace_root) / 'runtime' / environment.name / 'web'
    web_root.mkdir(parents=True, exist_ok=True)

    # --- Fix ownership BEFORE building ---
    # Gunicorn (root) writes files, celery (saasclaw) builds them.
    # Normalize ownership so build tools don't hit permission errors.
    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(Path(project.workspace_root) / 'runtime', log_file)

    # --- Framework-specific build env ---
    build_env = {}
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
    if output_path.exists():
        _publish_directory(output_path, web_root)
    else:
        # No build output dir — copy repo root (for plain HTML projects)
        _publish_directory(repo_path, web_root)

    environment.web_root = str(web_root)
    environment.save(update_fields=['web_root', 'updated_at'])

    # Set up nginx (using sudo)
    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"
    _ensure_nginx_static(service_name, environment.domain, str(web_root), log_file=log_file)



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

    # Set PORT env for the app
    env_file = runtime_root / '.env'
    env_lines = [f'PORT={port}', f'NODE_ENV=production']
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        env_lines.append(f'{ev.key}={ev.value}')
    env_file.write_text('\n'.join(env_lines) + '\n', encoding='utf-8')
    _normalize_ownership(env_file, log_file)

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


def _ensure_dotnet_sdk(log_file: Path) -> str:
    """Ensure .NET SDK is installed. Returns the dotnet binary path."""
    import shutil as _shutil
    dotnet = _shutil.which("dotnet")
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

    # Build app settings / env vars
    env_values = dict(existing_env)
    env_values.update({
        'ASPNETCORE_ENVIRONMENT': 'Development' if environment.name == 'preview' else 'Production',
        'ASPNETCORE_URLS': f'http://0.0.0.0:{environment.app_port}',
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

    # Restore and publish
    _run_command(f'{dotnet} restore', repo_path, log_file)
    publish_dir.mkdir(parents=True, exist_ok=True)
    _run_command(f'{dotnet} publish -c Release -o {publish_dir}', repo_path, log_file)

    # Normalize ownership of published output
    _normalize_ownership(publish_dir, log_file)

    # Write env file next to published dll so systemd can pick it up
    _write_text(publish_dir / '.env', _serialize_env_file(env_values))

    # Systemd service (always updated)
    _ensure_systemd_service(
        service_name=service_name,
        cwd=str(publish_dir),
        env_file=str(publish_dir / '.env'),
        exec_start=f'/usr/local/bin/dotnet {entrypoint_dll} --urls http://0.0.0.0:{environment.app_port}',
        description=f'SaaSClaw .NET app for {service_name}',
    )

    # Nginx proxy (always updated)
    _ensure_nginx_proxy(service_name, environment.domain, environment.app_port, log_file=log_file)

    # Start service
    _restart_service(service_name, log_file)

    # Healthcheck
    health_url = f'https://{environment.domain}{environment.healthcheck_path or "/health"}'
    _wait_for_http_healthcheck(health_url, log_file)

    environment.deploy_path = str(runtime_root)
    environment.save(update_fields=['deploy_path', 'updated_at'])
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

        _refresh_repo_checkout_for_deploy(project, repo_path, log_file)
        deployment.git_commit_sha = _repo_commit_sha(repo_path)
        deployment.save(update_fields=['git_commit_sha'])

        if environment.runtime_kind == Environment.RuntimeKind.DJANGO:
            _deploy_django_environment(project, environment, deployment, repo_path, log_file)
        elif environment.runtime_kind == Environment.RuntimeKind.NODE_SSR:
            _deploy_node_ssr_environment(project, environment, deployment, repo_path, log_file)
        elif environment.runtime_kind == Environment.RuntimeKind.DOTNET:
            _deploy_dotnet_environment(project, environment, deployment, repo_path, log_file)
        else:
            _deploy_static_environment(project, environment, deployment, repo_path, log_file)

        deployment.status = Deployment.Status.SUCCEEDED
        deployment.finished_at = dj_timezone.now()
        deployment.save(update_fields=['status', 'finished_at'])

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


def deploy_preview(project: Project, triggered_by=None) -> Deployment:
    """Deploy to the project's preview environment."""
    return _deploy_environment(project, 'preview', triggered_by=triggered_by)


def deploy_production(project: Project, triggered_by=None) -> Deployment:
    """Deploy to the project's production environment."""
    return _deploy_environment(project, 'production', triggered_by=triggered_by)

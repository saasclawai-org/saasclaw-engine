"""Deploy infrastructure helpers — extracted from service.py.

Shared utilities for environment files, command execution, nginx config,
systemd services, healthchecks, and scanning.
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
            f'sudo chown -R {saasclaw_uid}:{saasclaw_gid} {path}',
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
    actual = _remote_repo_url(repo_path)
    if not actual:
        # Can't read remote (permission issue, no .git, etc.) — skip check
        return
    # Skip drift check if origin is a local bare repo (used for wizard-managed deploys)
    if actual.startswith('/'):
        return
    expected = _normalize_repo_url(actual)
    remote_expected = _normalize_repo_url(project.repo_url)
    if expected and remote_expected and expected != remote_expected:
        raise RuntimeError(f'Repo remote drift: expected {remote_expected}, found {expected}')


def _refresh_repo_checkout_for_deploy(project: Project, repo_path: Path, log_file: Path) -> None:
    """Clone or pull the project repo for deploy."""
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    if not (repo_path / '.git').exists():
        from saasclaw_engine.integrations.models import GitHubInstallation
        inst = GitHubInstallation.objects.filter(
            account_name=project.repo_owner
        ).first()
        if inst and project.repo_owner and project.repo_name:
            clone_or_update_repo(
                inst.installation_id, project.repo_owner, project.repo_name,
                project.repo_default_branch or 'main', str(repo_path),
            )
        elif project.repo_url:
            _run_command(f'git clone {project.repo_url} {repo_path}', repo_path.parent, log_file)
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
                ).first()
                if not inst:
                    logger.warning('No GitHub installation found for owner %s on project %s', project.repo_owner, project.slug)
                if inst:
                    token = create_installation_access_token(inst.installation_id)
                    authed_url = f'https://x-access-token:{token}@github.com/{project.repo_owner}/{project.repo_name}.git'
                    _run_command(f'git remote set-url origin {authed_url}', repo_path, log_file)
            except Exception:
                pass
        elif is_github:
            logger.warning('GitHub repo missing owner/repo for project %s, converting to SSH', project.slug)

        # If remote uses expired token or plain HTTPS, convert to SSH
        try:
            import subprocess as _sp2
            url_result = _sp2.run(['git', 'remote', 'get-url', 'origin'], cwd=str(repo_path), capture_output=True, text=True, timeout=5)
            remote_url = url_result.stdout.strip()
            if 'x-access-token' in remote_url or remote_url.startswith('https://github.com/'):
                m = re.search(r'github\.com[:/](.+?)(?:\.git)?$', remote_url)
                if m:
                    ssh_url = f'git@github.com:{m.group(1)}.git'
                    _run_command(f'git remote set-url origin {ssh_url}', repo_path, log_file)
        except Exception:
            pass

        # Clean __pycache__ to avoid permission issues on git reset
        _run_command('find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true', repo_path, log_file)
        _normalize_ownership(repo_path / '.git', log_file)

        git_ssh = 'GIT_SSH_COMMAND="ssh -i /srv/saasclaw/.ssh/id_ed25519_deploy -o StrictHostKeyChecking=no" '
        _run_command(f'{git_ssh}git fetch origin', repo_path, log_file)
        _run_command(f'{git_ssh}git checkout {branch}', repo_path, log_file)
        _run_command(f'{git_ssh}git reset --hard origin/{branch}', repo_path, log_file)


def _slugify_system_name(value: str) -> str:
    s = re.sub(r'[^a-z0-9-]', '-', value.lower()).strip('-') or 'app'
    return re.sub(r'-{2,}', '-', s)


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
                    cur.execute(
                        _sql.SQL('ALTER ROLE {} WITH PASSWORD {}').format(
                            _sql.Identifier(db_user),
                            _sql.Literal(db_password),
                        )
                    )
                    with log_file.open('a') as h:
                        h.write(f'[role exists, password updated: {db_user}]\n')
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



def _scan_for_secrets(repo_path: Path) -> list[str]:
    """Scan repo files for potential secrets/credentials. Returns list of findings.
    Does NOT block deploy — warnings only (NIST AI RMF MAP 2.3).
    """
    findings: list[str] = []
    patterns = [
        (r'AKIA[0-9A-Z]{16}', 'AWS Access Key'),
        (r'aws_secret_access_key\s*[=:>]\s*["\']?[A-Za-z0-9/+=]{40}', 'AWS Secret Key'),
        (r'ghp_[A-Za-z0-9]{36}', 'GitHub Personal Access Token'),
        (r'gho_[A-Za-z0-9]{36}', 'GitHub OAuth Token'),
        (r'glpat-[A-Za-z0-9\-]{20}', 'GitLab Token'),
        (r'-----BEGIN (RSA |EC )?PRIVATE KEY-----', 'Private Key'),
        (r'password\s*[=:>]\s*["\']?[A-Za-z0-9!@#$%^&*]{8,}', 'Password in config'),
        (r'(?:mysql|postgres|mongodb|redis)://[^:]+:[^@]+@', 'DB connection string with credentials'),
        (r'api[_-]?key\s*[=:>]\s*["\']?[A-Za-z0-9_\-]{20,}', 'API Key'),
        (r'sk-[A-Za-z0-9]{20,}', 'Secret Key (OpenAI-style)'),
    ]
    skip_dirs = {'.git', 'node_modules', '__pycache__', '.venv', 'vendor', '.next', 'dist', 'build'}
    try:
        for fpath in repo_path.rglob('*'):
            if any(part in skip_dirs for part in fpath.parts):
                continue
            if not fpath.is_file() or fpath.stat().st_size > 500_000:
                continue
            try:
                content = fpath.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            for pattern, label in patterns:
                for match in re.finditer(pattern, content):
                    rel = fpath.relative_to(repo_path)
                    findings.append(f'{label} found in {rel}:{content[:match.end()].count(chr(10))+1}')
    except Exception as exc:
        findings.append(f'Secret scan error: {exc}')
    return findings


def _scan_dependencies(repo_path: Path) -> list[str]:
    """Scan project dependencies for known vulnerabilities.
    For Node.js: runs npm audit --json and checks for high/critical CVEs.
    For Python: runs pip check.
    Does NOT block deploy — warnings only (NIST AI RMF MAP 2.3).
    """
    findings: list[str] = []
    # Node.js: npm audit
    pkg_json = repo_path / 'package.json'
    if pkg_json.exists():
        try:
            result = subprocess.run(
                ['npm', 'audit', '--json'], cwd=str(repo_path),
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode in (0, 1):  # 0=no vulns, 1=vulns found
                try:
                    audit = json.loads(result.stdout)
                    meta = audit.get('metadata', {})
                    for severity in ('critical', 'high'):
                        count = meta.get('vulnerabilities', {}).get(severity, 0)
                        if count:
                            findings.append(f'npm audit: {count} {severity} vulnerabilities')
                except (json.JSONDecodeError, KeyError):
                    pass
        except subprocess.TimeoutExpired:
            findings.append('npm audit timed out (30s)')
        except FileNotFoundError:
            findings.append('npm not found — skipped dependency audit')
        except Exception as exc:
            findings.append(f'npm audit error: {exc}')
    # Python: pip check
    req_txt = repo_path / 'requirements.txt'
    if req_txt.exists():
        try:
            result = subprocess.run(
                ['pip', 'check'], cwd=str(repo_path),
                capture_output=True, text=True, timeout=30,
            )
            if result.stdout.strip():
                findings.append(f'pip check issues: {result.stdout.strip()[:300]}')
        except subprocess.TimeoutExpired:
            findings.append('pip check timed out (30s)')
        except FileNotFoundError:
            findings.append('pip not found — skipped dependency check')
        except Exception as exc:
            findings.append(f'pip check error: {exc}')
    return findings



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
        base = settings.PREVIEW_BASE_DOMAIN
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




def _write_tmp_script(path: str, content: str) -> None:
    """Write a temporary shell script with proper permissions."""
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix='.sh')
    try:
        os.chmod(tmp, 0o755)
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        subprocess.run(['sudo', 'cp', tmp, path], capture_output=True, timeout=5)
    finally:
        os.unlink(tmp)


def _write_and_validate_nginx(site_name, nginx_content, log_file=None):
    """Write nginx config, validate with nginx -t, and reload.
    If validation fails, roll back only this config so we never leave nginx broken.
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

    # Enable site
    result = subprocess.run(
        ['sudo', 'ln', '-sfn', str(site_file), str(site_enabled)],
        capture_output=True, timeout=10,
    )

    # Validate — nginx -t checks ALL configs, not just ours.
    # Warnings on stderr are not errors; only check the return code.
    result = subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, timeout=10)
    if result.returncode != 0:
        error = (result.stderr.decode() + result.stdout.decode())[:500]
        msg = f"nginx config validation FAILED for {site_name}: {error}"
        _logger.error(msg)
        if log_file:
            with log_file.open('a', encoding='utf-8') as h:
                h.write(f"NGINX ERROR: {msg}\n")
        # Roll back only this config
        subprocess.run(['sudo', 'rm', '-f', str(site_enabled)], capture_output=True, timeout=5)
        subprocess.run(['sudo', 'rm', '-f', str(site_file)], capture_output=True, timeout=5)
        subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, timeout=10)
        subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)
        return False

    # Reload nginx
    result = subprocess.run(['sudo', 'systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)
    if result.returncode != 0:
        msg = f"nginx reload failed after writing {site_name}: {result.stderr.decode()[:200]}"
        _logger.error(msg)
        if log_file:
            with log_file.open('a', encoding='utf-8') as h:
                h.write(f"NGINX ERROR: {msg}\n")
        return False

    return True

def _ensure_nginx_spa_proxy(service_name, domain, port, frontend_root, static_root, log_file=None):
    """Write nginx config that serves a React SPA and proxies /api/ to Django."""
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
        '    listen [::]:443 ssl;',
        '    server_name ' + domain + ';',
        '',
        '    ssl_certificate ' + ssl_cert + ';',
        '    ssl_certificate_key ' + ssl_key + ';',
        '    include /etc/letsencrypt/options-ssl-nginx.conf;',
        '    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;',
        '',
        '    client_max_body_size 25m;',
        '',
        '    include /etc/nginx/snippets/saasclaw-preview-branding.conf;',
        '',
        '    root ' + frontend_root + ';',
        '    index index.html;',
        '',
        '    # Django static files (admin, DRF, etc.)',
        '    location /static/ {',
        '        alias ' + str(static_root) + '/;',
        '    }',
        '',
        '    # Django API + admin',
        '    location /api/ {',
        '        proxy_pass http://127.0.0.1:' + str(port) + ';',
        '        proxy_set_header Host $host;',
        '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        '        proxy_set_header X-Forwarded-Proto $scheme;',
        '        proxy_redirect off;',
        '    }',
        '',
        '    location /admin/ {',
        '        proxy_pass http://127.0.0.1:' + str(port) + ';',
        '        proxy_set_header Host $host;',
        '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        '        proxy_set_header X-Forwarded-Proto $scheme;',
        '        proxy_redirect off;',
        '    }',
        '',
        '    location /api-auth/ {',
        '        proxy_pass http://127.0.0.1:' + str(port) + ';',
        '        proxy_set_header Host $host;',
        '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        '        proxy_set_header X-Forwarded-Proto $scheme;',
        '        proxy_redirect off;',
        '    }',
        '',
        '    # Health check endpoint (needed for .NET, Django, etc.)',
        '    location = /health {',
        '        proxy_pass http://127.0.0.1:' + str(port) + ';',
        '        proxy_set_header Host $host;',
        '    }',
        '',
        '    # SPA fallback - serve index.html for all non-file routes',
        '    location / {',
        '        try_files $uri $uri/ /index.html;',
        '    }',
        '}',
        '',
    ])
    if not _write_and_validate_nginx(service_name, nginx_content, log_file=log_file):
        raise RuntimeError(f'Failed to write/validate SPA nginx config for {service_name}')


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
        '    include ' + ('/etc/nginx/snippets/saasclaw-staging-branding.conf' if 'staging.' in settings.PREVIEW_BASE_DOMAIN else '/etc/nginx/snippets/saasclaw-preview-branding.conf') + ';',
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
        '',
        '    # Proxy Form API to Django for static sites',
        '    location /api/forms/ {',
        '        proxy_pass http://127.0.0.1:8010;',
        '        proxy_set_header Host saasclaw.ai;',
        '        proxy_set_header X-Forwarded-Host $host;',
        '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        '        proxy_set_header X-Forwarded-Proto $scheme;',
        '        proxy_redirect off;',
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
        '    include ' + ('/etc/nginx/snippets/saasclaw-staging-branding.conf' if 'staging.' in settings.PREVIEW_BASE_DOMAIN else '/etc/nginx/snippets/saasclaw-preview-branding.conf') + ';',
        '',
        '    root ' + web_root + ';',
        '    index index.html;',
        '',
        '    location / {',
        '        try_files $uri $uri/ /index.html;',
        '    }',
        '',
        '    # Proxy Form API to Django for static sites',
        '    location /api/forms/ {',
        '        proxy_pass http://127.0.0.1:8010;',
        '        proxy_set_header Host saasclaw.ai;',
        '        proxy_set_header X-Forwarded-Host $host;',
        '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        '        proxy_set_header X-Forwarded-Proto $scheme;',
        '        proxy_redirect off;',
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




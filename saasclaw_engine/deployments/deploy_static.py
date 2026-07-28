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

    # --- Check angular.json for outputPath ---
    angular_json = repo_path / 'angular.json'
    if angular_json.exists():
        try:
            data = _json.loads(angular_json.read_text())
            projects = data.get('projects', {})
            for pname, pconf in projects.items():
                # Angular 19 application builder: outputPath is a direct string
                arch = pconf.get('architect', {}).get('build', {})
                opts = arch.get('options', {})
                out_path = opts.get('outputPath') or opts.get('outputPath', '')
                if out_path:
                    return str(out_path)
                # Older browser builder also uses outputPath
                if isinstance(out_path, dict):
                    # Angular 17+ may use object form
                    base = out_path.get('base', '')
                    if base:
                        return base
            # Angular default convention: dist/<project-name>/browser
            if projects:
                first_project = list(projects.keys())[0]
                return f'dist/{first_project}/browser'
        except Exception:
            pass

    # --- Check package.json build script ---
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
            if 'ng build' in build_script.lower() or '@angular' in str(data.get('dependencies', {})):
                # Angular fallback if no angular.json
                return 'dist'
        except Exception:
            pass

    # --- Fallback: pick first directory that exists ---
    candidates = ['dist/angular/browser', 'dist', 'web', 'build', 'out', '_site', '.next', '.output/public']
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

    # Inject Predator LLM credentials for projects with require_gateway enabled
    predator_env = {}
    if getattr(project, 'require_gateway', False):
        try:
            from saasclaw_engine.studio_models.models import ProviderKey
            pk = ProviderKey.objects.filter(provider='predator').first()
            if pk and pk.provider_data:
                pd = pk.provider_data
                predator_env = {
                    'PREDATOR_BASE_URL': 'https://proliant-vllm.criticalpathsecurity.io/v1',
                    'PREDATOR_MODEL': 'openai/gpt-oss-20b',
                    'PREDATOR_CLIENT_ID': pd.get('client_id', ''),
                    'PREDATOR_CLIENT_SECRET': pd.get('client_secret', ''),
                }
                env_values.setdefault('PREDATOR_BASE_URL', predator_env['PREDATOR_BASE_URL'])
                env_values.setdefault('PREDATOR_MODEL', predator_env['PREDATOR_MODEL'])
                env_values.setdefault('PREDATOR_CLIENT_ID', predator_env['PREDATOR_CLIENT_ID'])
                env_values.setdefault('PREDATOR_CLIENT_SECRET', predator_env['PREDATOR_CLIENT_SECRET'])
        except Exception:
            pass

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

    # --- Framework-specific build env ---
    build_env = {}

    # Also inject Predator creds into build_env so set-env.js / build scripts can read them
    if predator_env:
        build_env.update(predator_env)

    # --- Fix ownership BEFORE building ---
    # Gunicorn (root) writes files, celery (saasclaw) builds them.
    # Normalize ownership so build tools don't hit permission errors.
    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(Path(project.workspace_root) / 'runtime', log_file)

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

    # Angular 19 application builder creates a nested browser/ subdir
    # inside outputPath. If index.html is in output_path/browser/, use that.
    nested_browser = output_path / 'browser'
    if nested_browser.is_dir() and (nested_browser / 'index.html').exists():
        output_path = nested_browser
        with log_file.open('a', encoding='utf-8') as handle:
            handle.write(f'Detected Angular nested browser/ dir, using {output_path}\n')

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

    # Write project-specific proxy snippets (token-injected)
    from saasclaw_engine.deployments.models import EnvironmentVariable
    hubspot_token = ''
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        if ev.key == 'HUBSPOT_TOKEN':
            hubspot_token = ev.value

    # HubSpot proxy with token baked in (if configured)
    if hubspot_token:
        snippet_path = Path(f'/etc/nginx/snippets/{service_name}-hubspot.conf')
        snippet = "\n".join([
            'location /api/hubspot/ {',
            '    proxy_pass https://api.hubapi.com/;',
            '    proxy_ssl_server_name on;',
            '    proxy_set_header Host api.hubapi.com;',
            f'    proxy_set_header Authorization "Bearer {hubspot_token}";',
            '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
            '    proxy_set_header X-Forwarded-Proto $scheme;',
            '    proxy_http_version 1.1;',
            "    proxy_set_header Connection '';",
            '    proxy_buffering off;',
            '    proxy_cache off;',
            '    proxy_read_timeout 60s;',
            '}',
            '',
        ])
        try:
            import subprocess as _sp
            _sp.run(['sudo', 'tee', str(snippet_path)], input=snippet, text=True, capture_output=True, timeout=10)
            _sp.run(['sudo', 'chmod', '644', str(snippet_path)], capture_output=True, timeout=5)
            if log_file:
                with log_file.open('a', encoding='utf-8') as h:
                    h.write(f'Wrote HubSpot proxy snippet with token\n')
        except Exception as e:
            if log_file:
                with log_file.open('a', encoding='utf-8') as h:
                    h.write(f'WARNING: Could not write HubSpot snippet: {e}\n')

    # Build list of extra includes for nginx
    extra_includes = []
    if getattr(project, 'require_gateway', False):
        extra_includes.append('/etc/nginx/snippets/predator-proxy.conf')
    if hubspot_token:
        extra_includes.append(f'/etc/nginx/snippets/{service_name}-hubspot.conf')

    _ensure_nginx_static(service_name, environment.domain, str(web_root), log_file=log_file, extra_includes=extra_includes)




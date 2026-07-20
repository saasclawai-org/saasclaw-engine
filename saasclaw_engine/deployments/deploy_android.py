"""Android (Kotlin + Jetpack Compose) environment deployment.

Handles Android SDK setup, Gradle build, APK output, and serving via nginx
as a downloadable file + QR code landing page.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

from .deploy_infra import (
    _load_env_file, _serialize_env_file, _write_text, _normalize_ownership,
    _run_command, _slugify_system_name,
    _ensure_nginx_proxy, _wait_for_http_healthcheck,
    _write_and_validate_nginx,
)

if TYPE_CHECKING:
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.deployments.models import Deployment, Environment

logger = logging.getLogger(__name__)

# Android SDK paths
_ANDROID_SDK_ROOT = '/opt/android-sdk'
_ANDROID_CMDLINE_TOOLS = f'{_ANDROID_SDK_ROOT}/cmdline-tools/latest'
_ANDROID_PLATFORM = 'android-35'
_ANDROID_BUILD_TOOLS = '35.0.0'

# JDK path (shared with Java deploy)
_JAVA_HOME = '/usr/lib/jvm/java-21-openjdk-amd64'


def _ensure_android_sdk(log_file: Path) -> str:
    """Ensure Android SDK is installed. Returns the SDK root path."""
    sdkmanager = f'{_ANDROID_CMDLINE_TOOLS}/bin/sdkmanager'
    if os.path.isfile(sdkmanager) and os.access(sdkmanager, os.X_OK):
        with log_file.open('a', encoding='utf-8') as h:
            h.write(f'Using existing Android SDK: {_ANDROID_SDK_ROOT}\n')
        return _ANDROID_SDK_ROOT

    with log_file.open('a', encoding='utf-8') as h:
        h.write('Installing Android SDK command-line tools...\n')

    # Ensure JDK is available (sdkmanager needs Java)
    if not os.path.isfile(f'{_JAVA_HOME}/bin/java'):
        _run_command(
            'sudo apt-get update -qq && sudo apt-get install -y -qq openjdk-21-jdk-headless',
            Path('/tmp'), log_file,
        )

    # Download command-line tools
    _run_command(
        f'sudo mkdir -p {_ANDROID_CMDLINE_TOOLS}',
        Path('/tmp'), log_file,
    )
    _run_command(
        'sudo curl -sSL -o /tmp/cmdline-tools.zip '
        'https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip',
        Path('/tmp'), log_file,
    )
    _run_command(
        f'sudo unzip -qo /tmp/cmdline-tools.zip -d /tmp/android-cmdline && '
        f'sudo cp -r /tmp/android-cmdline/cmdline-tools/* {_ANDROID_CMDLINE_TOOLS}/ && '
        f'sudo rm -rf /tmp/android-cmdline /tmp/cmdline-tools.zip',
        Path('/tmp'), log_file,
    )

    # Accept licenses and install platform + build-tools
    env = os.environ.copy()
    env['ANDROID_SDK_ROOT'] = _ANDROID_SDK_ROOT
    env['ANDROID_HOME'] = _ANDROID_SDK_ROOT
    env['JAVA_HOME'] = _JAVA_HOME
    env['ANDROID_SDK_ROOT'] = _ANDROID_SDK_ROOT

    _run_command(
        f'yes | {sdkmanager} --sdk_root={_ANDROID_SDK_ROOT} '
        f'"platform-tools" "platforms;{_ANDROID_PLATFORM}" "build-tools;{_ANDROID_BUILD_TOOLS}" 2>&1 || true',
        Path('/tmp'), log_file, env=env,
    )

    with log_file.open('a', encoding='utf-8') as h:
        h.write(f'Android SDK installed: {_ANDROID_SDK_ROOT}\n')

    return _ANDROID_SDK_ROOT


def _deploy_android_environment(
    project: Project,
    environment: Environment,
    deployment: Deployment,
    repo_path: Path,
    log_file: Path,
) -> None:
    """Build an Android APK and serve it as a download."""
    runtime_root = Path(project.workspace_root) / 'runtime' / environment.name
    runtime_root.mkdir(parents=True, exist_ok=True)

    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(runtime_root, log_file)

    env_file = runtime_root / '.env'
    existing_env = _load_env_file(env_file)

    # Ensure Android SDK
    sdk_root = _ensure_android_sdk(log_file)

    # Build env vars
    repo_env_file = repo_path / '.env'
    repo_env = _load_env_file(repo_env_file) if repo_env_file.exists() else {}
    env_values = dict(existing_env)
    env_values.update(repo_env)
    env_values.update({
        'ANDROID_SDK_ROOT': sdk_root,
        'ANDROID_HOME': sdk_root,
        'JAVA_HOME': _JAVA_HOME,
    })

    # Merge user-defined environment variables
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        env_values[ev.key] = ev.value

    _write_text(env_file, _serialize_env_file(env_values))

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Android runtime root: {runtime_root}\n')
        handle.write(f'Android SDK: {sdk_root}\n')
        handle.write(f'Repo path: {repo_path}\n')

    # Set up environment for Gradle
    gradle_env = os.environ.copy()
    gradle_env.update({
        'ANDROID_SDK_ROOT': sdk_root,
        'ANDROID_HOME': sdk_root,
        'JAVA_HOME': _JAVA_HOME,
    })

    # Make gradlew executable
    gradlew = repo_path / 'gradlew'
    if gradlew.exists():
        _run_command(f'chmod +x {gradlew}', repo_path, log_file)
    else:
        # Generate gradle wrapper if missing
        _run_command(
            f'curl -sSL -o {repo_path}/gradlew https://raw.githubusercontent.com/gradle/gradle/v8.11.1/gradlew && '
            f'chmod +x {repo_path}/gradlew && '
            f'mkdir -p {repo_path}/gradle/wrapper && '
            f'curl -sSL -o {repo_path}/gradle/wrapper/gradle-wrapper.jar https://github.com/gradle/gradle/raw/v8.11.1/gradle/wrapper/gradle-wrapper.jar',
            repo_path, log_file, timeout=60,
        )

    # Make applicationId unique per project to avoid signature conflicts
    # when multiple SaaSClaw Android apps are installed on the same device
    build_gradle = repo_path / 'app' / 'build.gradle.kts'
    if build_gradle.exists():
        with open(build_gradle, 'r') as bf:
            bg = bf.read()
        unique_id = f'com.saasclaw.{project.slug.replace("-", "_")}'
        if f'applicationId = "com.saasclaw.app"' in bg:
            bg = bg.replace(
                'applicationId = "com.saasclaw.app"',
                f'applicationId = "{unique_id}"'
            )
            with open(build_gradle, 'w') as bf:
                bf.write(bg)
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write(f'Set applicationId to {unique_id}\n')

    # Fix app display name: derive from project name instead of hardcoded "SaaSClaw"
    strings_xml = repo_path / 'app' / 'src' / 'main' / 'res' / 'values' / 'strings.xml'
    if strings_xml.exists():
        with open(strings_xml, 'r') as sf:
            sx = sf.read()
        if '>SaaSClaw<' in sx:
            # Convert slug to readable name (e.g. "workout-tracker" → "Workout Tracker")
            display_name = project.name or project.slug.replace('-', ' ').title()
            sx = sx.replace('>SaaSClaw<', f'>{display_name}<')
            with open(strings_xml, 'w') as sf:
                sf.write(sx)
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write(f'Set app display name to "{display_name}"\n')

    # Build debug APK (works without signing config)
    with log_file.open('a', encoding='utf-8') as handle:
        handle.write('Building debug APK...\n')

    _run_command(
        f'./gradlew assembleDebug --no-daemon -q',
        repo_path, log_file, env=gradle_env, timeout=600,
    )

    # Find the built APK
    apk_dir = repo_path / 'app' / 'build' / 'outputs' / 'apk' / 'debug'
    if not apk_dir.is_dir():
        # Try alternative paths
        for pattern in ['**/build/outputs/apk/debug/*.apk', '**/outputs/apk/debug/*.apk']:
            apks = list(repo_path.glob(pattern))
            if apks:
                apk_dir = apks[0].parent
                break

    if not apk_dir.is_dir() or not any(apk_dir.glob('*.apk')):
        raise RuntimeError(
            f'No APK found after Gradle build. Checked {apk_dir}'
        )

    apks = list(apk_dir.glob('*.apk'))
    apk_path = apks[0]
    apk_filename = apk_path.name

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Built APK: {apk_filename}\n')

    # Copy APK to runtime root
    runtime_apk = runtime_root / apk_filename
    shutil.copy2(apk_path, runtime_apk)
    _normalize_ownership(runtime_root, log_file)

    # Create a download landing page
    landing_html = f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{project.name} — Download APK</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; display: grid; place-items: center; padding: 24px; }}
    .card {{ max-width: 480px; width: 100%; text-align: center; }}
    .icon {{ font-size: 4rem; margin-bottom: 16px; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 8px; }}
    p {{ color: #94a3b8; font-size: 0.95rem; line-height: 1.6; margin-bottom: 24px; }}
    .download-btn {{ display: inline-flex; align-items: center; gap: 8px; padding: 14px 28px; border-radius: 12px; font-weight: 700; font-size: 1rem; text-decoration: none; background: linear-gradient(135deg, #3b82f6, #8b5cf6); color: white; transition: transform 0.15s; }}
    .download-btn:hover {{ transform: translateY(-2px); }}
    .info {{ margin-top: 24px; font-size: 0.82rem; color: #64748b; }}
    .info code {{ background: #1e293b; padding: 2px 6px; border-radius: 4px; color: #cbd5e1; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">📱</div>
    <h1>{project.name}</h1>
    <p>Android app built with SaaSClaw. Download the APK below and install on your device.</p>
    <a href="/{apk_filename}" class="download-btn" download>⬇ Download APK</a>
    <div class="info">
      <p>File: <code>{apk_filename}</code></p>
      <p>Build: Debug · API 35 (Android 15)</p>
      <p>Built with SaaSClaw · <a href="https://saasclaw.ai" style="color:#3b82f6;">saasclaw.ai</a></p>
    </div>
  </div>
</body>
</html>'''
    _write_text(runtime_root / 'index.html', landing_html)

    # Configure nginx to serve the APK + landing page
    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"

    # Write nginx config for static file serving (APK + landing page)
    nginx_conf = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {environment.domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {environment.domain};

    ssl_certificate /etc/letsencrypt/live/preview.saasclaw.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/preview.saasclaw.ai/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    root {runtime_root};
    index index.html;

    location / {{
        try_files $uri $uri/ =404;
    }}

    location ~ \\.apk$ {{
        types {{ application/vnd.android.package-archive apk; }}
        add_header Content-Disposition attachment;
    }}
}}
"""
    if not _write_and_validate_nginx(service_name, nginx_conf, log_file):
        raise RuntimeError(f'Failed to configure nginx for {service_name}')

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'APK served at: https://{environment.domain}/{apk_filename}\n')
        handle.write(f'Landing page: https://{environment.domain}/\n')

    environment.deploy_path = str(runtime_root)
    environment.save(update_fields=['deploy_path', 'updated_at'])

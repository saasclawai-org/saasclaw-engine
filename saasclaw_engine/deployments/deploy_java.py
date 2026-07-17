"""Java / Spring Boot environment deployment.

Handles JDK detection/installation, Maven build, JAR execution via systemd,
and nginx reverse proxy configuration.
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
    _run_command, _slugify_system_name, _ensure_app_port,
    _ensure_systemd_service, _ensure_nginx_proxy,
    _ensure_postgres_database, _wait_for_http_healthcheck, _restart_service,
)

if TYPE_CHECKING:
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.deployments.models import Deployment, Environment

logger = logging.getLogger(__name__)

# JDK / Maven paths
_JAVA_HOME = '/usr/lib/jvm/java-21-openjdk-amd64'
_JAVA_BIN = f'{_JAVA_HOME}/bin/java'
_MAVEN_HOME = '/opt/maven'
_MAVEN_BIN = f'{_MAVEN_HOME}/bin/mvn'


def _ensure_jdk(log_file: Path) -> str:
    """Ensure JDK 21 is installed. Returns the java binary path."""
    if os.path.isfile(_JAVA_BIN) and os.access(_JAVA_BIN, os.X_OK):
        with log_file.open('a', encoding='utf-8') as h:
            h.write(f'Using existing JDK: {_JAVA_BIN}\n')
            import subprocess
            r = subprocess.run([_JAVA_BIN, '-version'], capture_output=True, text=True, timeout=15)
            h.write(f'Java version: {r.stderr.strip()}\n')
        return _JAVA_BIN

    with log_file.open('a', encoding='utf-8') as h:
        h.write('Installing JDK 21 (OpenJDK)...\n')

    _run_command(
        'sudo apt-get update -qq && sudo apt-get install -y -qq openjdk-21-jdk-headless',
        Path('/tmp'), log_file,
    )
    return _JAVA_BIN


def _ensure_maven(log_file: Path) -> str:
    """Ensure Apache Maven is installed. Returns the mvn binary path."""
    if os.path.isfile(_MAVEN_BIN) and os.access(_MAVEN_BIN, os.X_OK):
        with log_file.open('a', encoding='utf-8') as h:
            h.write(f'Using existing Maven: {_MAVEN_BIN}\n')
        return _MAVEN_BIN

    with log_file.open('a', encoding='utf-8') as h:
        h.write('Installing Apache Maven 3.9.16...\n')

    # Download and install Maven
    _run_command(
        'sudo curl -sSL -o /tmp/maven.tar.gz '
        'https://dlcdn.apache.org/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.tar.gz',
        Path('/tmp'), log_file,
    )
    _run_command(
        f'sudo mkdir -p {_MAVEN_HOME} && '
        f'sudo tar xzf /tmp/maven.tar.gz -C {_MAVEN_HOME} --strip-components=1',
        Path('/tmp'), log_file,
    )
    _run_command(
        f'sudo ln -sf {_MAVEN_BIN} /usr/local/bin/mvn',
        Path('/'), log_file,
    )
    return _MAVEN_BIN


def _detect_main_module(repo_path: Path) -> str | None:
    """Detect the module that contains the Spring Boot application (has @SpringBootApplication).

    Returns the module directory name, or None for a single-module project.
    """
    # Check if this is a multi-module project (parent pom.xml with <modules>)
    parent_pom = repo_path / 'pom.xml'
    if not parent_pom.is_file():
        return None

    content = parent_pom.read_text(errors='replace')

    # If no <modules> section, it's a single-module project
    if '<modules>' not in content:
        return None

    # Multi-module: find which module has the Spring Boot main class
    for pom_file in repo_path.glob('*/pom.xml'):
        module_dir = pom_file.parent
        # Check for @SpringBootApplication in Java files
        for java_file in module_dir.rglob('*.java'):
            try:
                java_content = java_file.read_text(errors='replace')
                if '@SpringBootApplication' in java_content:
                    return module_dir.name
            except Exception:
                continue

    # Fallback: look for spring-boot-maven-plugin in a module
    for pom_file in repo_path.glob('*/pom.xml'):
        content = pom_file.read_text(errors='replace')
        if 'spring-boot-maven-plugin' in content:
            return pom_file.parent.name

    return None


def _detect_jar_name(repo_path: Path, module_name: str | None) -> str:
    """Detect the built JAR file name from pom.xml."""
    if module_name:
        pom_path = repo_path / module_name / 'pom.xml'
    else:
        pom_path = repo_path / 'pom.xml'

    if pom_path.is_file():
        import re
        content = pom_path.read_text(errors='replace')

        # Try to extract artifactId and version
        artifact_match = re.search(r'<artifactId>([^<]+)</artifactId>', content)
        version_match = re.search(r'<version>([^<]+)</version>', content)

        if artifact_match:
            artifact = artifact_match.group(1)
            # If version is a property reference, use a generic pattern
            if version_match:
                version = version_match.group(1)
                # Handle ${revision} or property refs
                if version.startswith('${'):
                    # Try to find the property
                    prop_name = version[2:-1]
                    prop_match = re.search(
                        rf'<{prop_name}>([^<]+)</{prop_name}>', content
                    )
                    version = prop_match.group(1) if prop_match else '*'
            else:
                version = '*'

            # Spring Boot repackages as artifactId-version.jar (not .jar.original)
            return f'{artifact}-{version}.jar'

    # Fallback
    return '*.jar'


def _deploy_java_environment(
    project: Project,
    environment: Environment,
    deployment: Deployment,
    repo_path: Path,
    log_file: Path,
) -> None:
    """Deploy a Java / Spring Boot app to an environment."""
    _ensure_app_port(environment)
    runtime_root = Path(project.workspace_root) / 'runtime' / environment.name
    runtime_root.mkdir(parents=True, exist_ok=True)

    _normalize_ownership(repo_path, log_file)
    _normalize_ownership(runtime_root, log_file)

    env_file = runtime_root / '.env'
    existing_env = _load_env_file(env_file)
    service_name = f"saasclaw-{_slugify_system_name(project.slug)}-{environment.name}"

    # Ensure JDK + Maven
    java_bin = _ensure_jdk(log_file)
    mvn_bin = _ensure_maven(log_file)

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

    # JDBC connection string for Spring Boot
    jdbc_url = (
        f'jdbc:postgresql://{db_host}:{db_port}/{db_name}'
    )
    database_url = (
        f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
    )

    # Build env vars
    repo_env_file = repo_path / '.env'
    repo_env = _load_env_file(repo_env_file) if repo_env_file.exists() else {}
    env_values = dict(existing_env)
    env_values.update(repo_env)
    env_values.update({
        'SPRING_PROFILES_ACTIVE': 'prod' if environment.name == 'production' else 'dev',
        'SERVER_PORT': str(environment.app_port),
        'SPRING_DATASOURCE_URL': jdbc_url,
        'SPRING_DATASOURCE_USERNAME': db_user,
        'SPRING_DATASOURCE_PASSWORD': db_password,
        'SPRING_DATASOURCE_DRIVER_CLASS_NAME': 'org.postgresql.Driver',
        'SPRING_JPA_HIBERNATE_DDL_AUTO': 'update',
        'SPRING_JPA_DATABASE_PLATFORM': 'org.hibernate.dialect.PostgreSQLDialect',
        'POSTGRES_DB': db_name,
        'POSTGRES_USER': db_user,
        'POSTGRES_PASSWORD': db_password,
        'POSTGRES_HOST': db_host,
        'POSTGRES_PORT': db_port,
        'DATABASE_URL': database_url,
        'JAVA_HOME': _JAVA_HOME,
    })

    # Merge user-defined environment variables
    from saasclaw_engine.deployments.models import EnvironmentVariable
    for ev in EnvironmentVariable.objects.filter(environment=environment):
        env_values[ev.key] = ev.value

    _write_text(env_file, _serialize_env_file(env_values))

    # Detect module structure
    main_module = _detect_main_module(repo_path)
    jar_name = _detect_jar_name(repo_path, main_module)

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Java runtime root: {runtime_root}\n')
        handle.write(f'Java env file: {env_file}\n')
        handle.write(f'Java main module: {main_module or "(single-module)"}\n')
        handle.write(f'Java JAR pattern: {jar_name}\n')
        handle.write(f'Java app port: {environment.app_port}\n')
        handle.write(f'Java service name: {service_name}\n')

    # Build with Maven
    build_dir = repo_path / main_module if main_module else repo_path

    # Set JAVA_HOME for Maven
    maven_env = os.environ.copy()
    maven_env['JAVA_HOME'] = _JAVA_HOME

    _run_command(
        f'{mvn_bin} clean package -DskipTests -q',
        build_dir, log_file, env=maven_env,
    )

    # Find the built JAR
    target_dir = build_dir / 'target'
    if not target_dir.is_dir():
        raise RuntimeError(
            f'Maven build did not produce target/ directory in {build_dir}'
        )

    # Find the JAR (exclude .jar.original which is the pre-repackage version)
    import glob as _glob
    jar_pattern = jar_name.replace('*', '*')
    jars = [
        f for f in target_dir.glob('*.jar')
        if not f.name.endswith('.jar.original')
        and not f.name.endswith('-sources.jar')
        and not f.name.endswith('-javadoc.jar')
    ]

    if not jars:
        raise RuntimeError(
            f'No JAR file found in {target_dir} after Maven build'
        )

    # If multiple JARs, prefer the one matching our pattern
    if len(jars) > 1 and jar_name != '*.jar':
        name_part = jar_name.replace('*', '').rstrip('-')
        matching = [j for j in jars if name_part in j.name]
        if matching:
            jars = matching

    jar_path = jars[0]
    jar_filename = jar_path.name

    with log_file.open('a', encoding='utf-8') as handle:
        handle.write(f'Built JAR: {jar_filename}\n')

    # Copy JAR to runtime root
    runtime_jar = runtime_root / jar_filename
    shutil.copy2(jar_path, runtime_jar)
    _normalize_ownership(runtime_root, log_file)

    # Write env file next to JAR for systemd
    _write_text(runtime_root / '.env', _serialize_env_file(env_values))

    # Determine health check path
    healthcheck_path = environment.healthcheck_path or '/actuator/health'
    if not healthcheck_path.startswith('/'):
        healthcheck_path = '/' + healthcheck_path

    # Systemd service
    _ensure_systemd_service(
        service_name=service_name,
        cwd=str(runtime_root),
        env_file=str(runtime_root / '.env'),
        exec_start=f'{java_bin} -jar {jar_filename} --server.port={environment.app_port}',
        description=f'SaaSClaw Java/Spring Boot app for {service_name}',
    )

    # Nginx reverse proxy
    _ensure_nginx_proxy(service_name, environment.domain, environment.app_port, log_file=log_file)

    # Start service
    _restart_service(service_name, log_file)

    # Healthcheck
    health_url = f'https://{environment.domain}{healthcheck_path}'
    _wait_for_http_healthcheck(health_url, log_file)

    environment.deploy_path = str(runtime_root)
    environment.save(update_fields=['deploy_path', 'updated_at'])

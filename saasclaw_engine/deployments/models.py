from django.conf import settings
from django.db import models

from saasclaw_engine.projects.models import Project


class CustomDomain(models.Model):
    """A custom domain mapped to a project's production environment."""

    class Status(models.TextChoices):
        PENDING_DNS = 'pending_dns', 'Pending DNS'
        VERIFYING = 'verifying', 'Verifying'
        SSL_REQUESTING = 'ssl_requesting', 'Requesting SSL'
        ACTIVE = 'active', 'Active'
        FAILED = 'failed', 'Failed'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='custom_domains')
    domain = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_DNS)
    dns_verified_at = models.DateTimeField(null=True, blank=True)
    ssl_cert_path = models.CharField(max_length=500, blank=True)
    ssl_key_path = models.CharField(max_length=500, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.domain} ({self.status})'


class Environment(models.Model):
    class Name(models.TextChoices):
        PREVIEW = 'preview', 'Preview'
        PRODUCTION = 'production', 'Production'

    class RuntimeKind(models.TextChoices):
        STATIC = 'static', 'Static'
        NODE_STATIC = 'node_static', 'Node Static'
        NODE_SSR = 'node_ssr', 'Node SSR'
        DJANGO = 'django', 'Django'
        DOTNET = 'dotnet', '.NET'
        JAVA = 'java', 'Java / Spring Boot'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='environments')
    name = models.CharField(max_length=20, choices=Name.choices)
    slug = models.SlugField()
    domain = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    deploy_path = models.CharField(max_length=500, blank=True)
    web_root = models.CharField(max_length=500, blank=True)
    install_command = models.CharField(max_length=255, blank=True)
    build_command = models.CharField(max_length=255, blank=True)
    output_directory = models.CharField(max_length=255, blank=True)
    runtime_kind = models.CharField(max_length=20, choices=RuntimeKind.choices, default=RuntimeKind.STATIC)
    app_port = models.PositiveIntegerField(null=True, blank=True)
    healthcheck_path = models.CharField(max_length=255, blank=True)
    python_entrypoint = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('project', 'name')]
        ordering = ['project_id', 'name']


class EnvironmentVariable(models.Model):
    """A key-value environment variable scoped to a project + environment."""
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='env_variables',
    )
    environment = models.ForeignKey(
        Environment,
        on_delete=models.CASCADE,
        related_name='variables',
    )
    key = models.CharField(max_length=255)
    value = models.TextField(blank=True)
    is_secret = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('environment', 'key')]
        ordering = ['key']

    def __str__(self):
        return f"{self.key}={'***' if self.is_secret else self.value[:20]}"

    def __str__(self):
        return f'{self.project.slug}:{self.name}'


class Deployment(models.Model):
    class Source(models.TextChoices):
        AGENT = 'agent', 'Agent'
        MANUAL = 'manual', 'Manual'
        SYSTEM = 'system', 'System'
        GIT_PUSH = 'git_push', 'Git Push'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELED = 'canceled', 'Canceled'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='deployments')
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, related_name='deployments')
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='deployments_triggered')
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    git_branch = models.CharField(max_length=100, blank=True)
    git_commit_sha = models.CharField(max_length=64, blank=True)
    git_commit_message = models.TextField(blank=True)
    artifact_object_key = models.CharField(max_length=500, blank=True)
    build_log_object_key = models.CharField(max_length=500, blank=True)
    deploy_log_object_key = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    manifest_object_key = models.CharField(max_length=500, blank=True)
    metadata_json = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.project.slug}:{self.environment.name}:{self.status}'

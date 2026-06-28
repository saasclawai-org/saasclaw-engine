from django.conf import settings
from django.db import models

from saasclaw_engine.projects.models import Project


class AgentTask(models.Model):
    class TaskType(models.TextChoices):
        PLAN = 'plan', 'Plan'
        EDIT_CODE = 'edit_code', 'Edit App'
        CREATE_RESOURCE = 'create_resource', 'Create Resource'
        GENERATE_SITE = 'generate_site', 'Generate Site'
        FIX_BUG = 'fix_bug', 'Fix Bug'
        INSPECT_REPO = 'inspect_repo', 'Inspect Repo'
        DEPLOY_PREVIEW = 'deploy_preview', 'Deploy Preview'
        DEPLOY_PRODUCTION = 'deploy_production', 'Deploy Production'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELED = 'canceled', 'Canceled'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='agent_tasks')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agent_tasks_requested')
    parent_task = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='followups')
    thread_key = models.CharField(max_length=64, blank=True)
    task_type = models.CharField(max_length=32, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    prompt = models.TextField(blank=True)
    system_summary = models.TextField(blank=True)
    result_summary = models.TextField(blank=True)
    session_key = models.CharField(max_length=255, blank=True)
    linked_branch = models.CharField(max_length=100, blank=True)
    linked_commit_sha = models.CharField(max_length=64, blank=True)
    log_object_key = models.CharField(max_length=500, blank=True)
    metadata_json = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.project.slug}:{self.task_type}:{self.status}'


class AgentTaskAttachment(models.Model):
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='attachments')
    original_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.task_id}:{self.original_name}'

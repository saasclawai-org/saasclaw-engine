"""Studio models — workspaces, agent sessions, messages, and provider keys."""
import uuid

from django.conf import settings
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


PROVIDER_CHOICES = [
    ('zai', 'Z.ai'),
    ('openai', 'OpenAI'),
    ('anthropic', 'Anthropic'),
]

PROVIDER_MODELS = {
    'zai': ['glm-5.2', 'glm-5.1', 'glm-5-turbo', 'glm-4.7', 'glm-4.5-air'],
    'openai': ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'o3', 'o4-mini'],
    'anthropic': ['claude-sonnet-4-20250514', 'claude-opus-4-20250515', 'claude-haiku-4-20250414'],
}


class ProviderKey(models.Model):
    """A user's API key for an LLM provider."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='provider_keys')
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    api_key = models.CharField(max_length=500)
    default_model = models.CharField(max_length=100, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'provider')]
        ordering = ['provider']

    def __str__(self):
        key = self.api_key or ''
        return f"{self.provider}: {key[:8]}...{key[-4:] if len(key) > 12 else '***'}"


class Workspace(models.Model):
    """A working copy of a project repo for agent editing.

    Uses git worktree to create an isolated checkout without cloning.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="workspaces"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspaces"
    )
    base_branch = models.CharField(max_length=200, default="main")
    work_branch = models.CharField(max_length=200, blank=True)
    local_path = models.CharField(max_length=500, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.project.slug} @ {self.work_branch or self.base_branch}"

    @property
    def branch(self):
        return self.work_branch or self.base_branch


class AgentProfile(models.Model):
    """A reusable agent persona with its own prompt, tools, and model preference."""
    EMOJI_CHOICES = [
        ('🤖', 'Pi'),
        ('🏗️', 'Builder'),
        ('🔧', 'Custom'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    emoji = models.CharField(max_length=10, default='🏗️')
    description = models.CharField(max_length=200, blank=True)
    system_prompt = models.TextField(blank=True, help_text='Extra instructions injected into the agent prompt')
    allowed_tools = models.JSONField(default=list, blank=True, help_text='Empty list = all tools allowed')
    suggested_provider = models.CharField(max_length=20, blank=True, default='')
    suggested_model = models.CharField(max_length=100, blank=True, default='')
    is_default = models.BooleanField(default=False, help_text='Show in default agent tabs')
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return f'{self.emoji} {self.name}'


class Todo(models.Model):
    """A todo item for a project, updated by the agent as it works."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="todos"
    )
    text = models.CharField(max_length=500)
    done = models.BooleanField(default=False)
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "-created_at"]


class AgentSession(models.Model):
    """A conversation session between a user and the coding agent."""
    STATUS_CHOICES = [
        ("idle", "Idle"),
        ("running", "Running"),
        ("ended", "Ended"),
    ]
    STAGE_CHOICES = [
        ("chat", "Chat"),
    ]
    STAGE_ORDER = ["chat"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="sessions"
    )
    workspace = models.ForeignKey(
        Workspace, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sessions"
    )
    title = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True, default='')
    profile = models.ForeignKey(
        AgentProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions"
    )
    stage = models.CharField(max_length=10, choices=STAGE_CHOICES, default="chat")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="idle")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Session {self.id.hex[:8]}"


class AgentMessage(models.Model):
    """Messages in an agent session — user, assistant, or tool results."""
    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
        ("tool", "Tool"),
        ("system", "System"),
    ]

    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    tool_call = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:80]}"


class TokenUsage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='token_usage')
    session = models.ForeignKey(AgentSession, on_delete=models.SET_NULL, null=True, blank=True, related_name='token_usage')
    user = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    
    provider = models.CharField(max_length=20)
    model = models.CharField(max_length=60)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    profile = models.CharField(max_length=40, blank=True, default='')
    stage = models.CharField(max_length=10, blank=True, default='')
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.provider}/{self.model} — {self.total_tokens} tokens"


class SiteSettings(models.Model):
    """Singleton model for platform-wide staff-configurable settings."""

    # Project approval
    project_approval_required = models.BooleanField(
        default=False,
        help_text='When enabled, users must submit a project request that staff approves before creating projects.'
    )

    # Deploy security scanning
    secret_scan_enabled = models.BooleanField(
        default=True,
        help_text='Scan committed code for secrets (AWS keys, tokens, private keys) during deploy.'
    )
    dependency_scan_enabled = models.BooleanField(
        default=True,
        help_text='Run npm audit / pip check during deploy for known vulnerabilities.'
    )
    block_deploy_on_findings = models.BooleanField(
        default=False,
        help_text='Block deploy when high/critical security findings are detected (advisory by default).'
    )

    # AI governance
    default_require_gateway = models.BooleanField(
        default=False,
        help_text='New projects default to LLM gateway mode (data stays on-server).'
    )
    ai_disclosure_required = models.BooleanField(
        default=True,
        help_text='Require AI disclosure checkbox on project intake form.'
    )
    pii_guard_enabled = models.BooleanField(
        default=True,
        help_text='Redact PII (SSNs, credit cards, etc.) before sending to LLM providers.'
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        help_text='Last user to update settings.'
    )

    class Meta:
        app_label = 'studio_models'
        verbose_name = 'Site Settings'
        verbose_name_plural = 'Site Settings'

    @classmethod
    def get(cls):
        """Get or create the singleton settings instance."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1  # Enforce singleton
        super().save(*args, **kwargs)

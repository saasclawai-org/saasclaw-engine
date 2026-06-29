from django.conf import settings
from django.db import models


class Project(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Active'
        ARCHIVED = 'archived', 'Archived'
        SUSPENDED = 'suspended', 'Suspended'

    class Framework(models.TextChoices):
        HTML = 'html', 'HTML'
        VITE_REACT = 'vite_react', 'Vite React'
        ASTRO = 'astro', 'Astro'
        NEXT_STATIC = 'next_static', 'Next.js Static'
        DJANGO = 'django', 'Django App'

    class RepoProvider(models.TextChoices):
        GITHUB = 'github', 'GitHub'

    class RiskTier(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    class OnboardingStep(models.TextChoices):
        START = 'start', 'Start'
        GITHUB = 'github', 'GitHub'
        GENERATE = 'generate', 'Generate'
        BUILDING = 'building', 'Building'
        READY = 'ready', 'Ready'
        DONE = 'done', 'Done'

    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='owned_projects')
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    framework = models.CharField(max_length=32, choices=Framework.choices, default=Framework.HTML)
    template_key = models.CharField(max_length=64, blank=True)
    repo_provider = models.CharField(max_length=20, choices=RepoProvider.choices, default=RepoProvider.GITHUB)
    repo_owner = models.CharField(max_length=255, blank=True)
    repo_name = models.CharField(max_length=255, blank=True)
    repo_url = models.URLField(blank=True)
    repo_default_branch = models.CharField(max_length=100, default='main')
    github_installation_id = models.BigIntegerField(null=True, blank=True)
    github_repo_id = models.BigIntegerField(null=True, blank=True)
    workspace_root = models.CharField(max_length=500, blank=True)
    preview_domain = models.CharField(max_length=255, blank=True)
    production_domain = models.CharField(max_length=255, blank=True)
    onboarding_step = models.CharField(max_length=32, choices=OnboardingStep.choices, blank=True, default=OnboardingStep.START)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)
    onboarding_goal_prompt = models.TextField(blank=True)
    notes = models.TextField(blank=True, help_text='Project notes and context')
    directives = models.TextField(blank=True, help_text='Standing instructions for the agent')
    require_gateway = models.BooleanField(default=False, help_text='When true, agent must use local/gateway LLM endpoint — cloud providers blocked')
    hugo_theme = models.CharField(max_length=128, blank=True, help_text='Hugo theme name (for Hugo framework projects)')
    context_cache = models.TextField(blank=True, help_text='Cached project context for wizard')
    context_cache_updated_at = models.DateTimeField(null=True, blank=True)
    risk_tier = models.CharField(max_length=10, choices=RiskTier.choices, default=RiskTier.LOW)
    last_deployed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def update_risk_tier(self, data_sensitivity: str = '') -> str:
        """Auto-assign risk tier based on data sensitivity classification.

        Mapping (NIST AI RMF): none→low, low→low, medium→medium,
        high+PII→high, high+PHI→critical, high+financial→high.
        Returns the assigned tier.
        """
        ds = (data_sensitivity or '').strip().lower()
        if ds in ('none', '', 'low'):
            self.risk_tier = self.RiskTier.LOW
        elif ds == 'medium':
            self.risk_tier = self.RiskTier.MEDIUM
        elif ds == 'high':
            # Default to high; refine if we know data type from description
            desc = (self.description or '').lower()
            if any(kw in desc for kw in ('phi', 'health', 'medical', 'hipaa')):
                self.risk_tier = self.RiskTier.CRITICAL
            else:
                self.risk_tier = self.RiskTier.HIGH
        else:
            self.risk_tier = self.RiskTier.LOW
        self.save(update_fields=['risk_tier', 'updated_at'])
        return self.risk_tier

    def __str__(self):
        return self.name


class WaitingList(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    referred_by = models.CharField(max_length=255, blank=True, default='direct')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.email


class ProjectSubmission(models.Model):
    """A project creation request pending staff approval.

    When PROJECT_APPROVAL_REQUIRED is True, users must submit a request
    describing what they want to build. Staff review and approve before
    the project is created.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="project_submissions")

    name = models.CharField(max_length=128, help_text="Proposed project name")
    slug = models.SlugField(max_length=128, help_text="URL-safe project slug")
    description = models.TextField(blank=True, help_text="What the project does, goals, audience")
    framework = models.CharField(max_length=64, default="html", help_text="Desired framework/template")
    source = models.CharField(max_length=64, default="blank", help_text="blank, template, or github")
    template = models.CharField(max_length=64, blank=True, help_text="Template name if applicable")
    repo_url = models.URLField(blank=True, help_text="GitHub URL if importing")

    business_justification = models.TextField(
        blank=True,
        help_text="Why this project is needed, who will use it, compliance requirements"
    )
    data_sensitivity = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Data sensitivity level: none, low, medium, high (PII/PHI)"
    )
    estimated_timeline = models.CharField(
        max_length=128, blank=True,
        help_text="When they need it, urgency level"
    )
    ai_generated_code = models.BooleanField(
        default=True,
        help_text="Whether this project uses AI-generated code (NIST AI RMF disclosure)"
    )

    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_submissions"
    )
    staff_notes = models.TextField(blank=True, help_text="Internal staff notes")
    require_gateway = models.BooleanField(
        default=False,
        help_text="Staff can pre-set gateway requirement on approval"
    )
    approved_project = models.OneToOneField(
        "Project", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="submission"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Project Submission"

    def __str__(self):
        return f"{self.name} ({self.status}) — by {self.requester.username}"

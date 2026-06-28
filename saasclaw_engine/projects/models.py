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
    last_deployed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

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

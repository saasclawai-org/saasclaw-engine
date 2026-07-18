from django.conf import settings
from django.db import models


class GitHubInstallation(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='github_installations',
        null=True,
        blank=True,
        help_text='The SaaSClaw user who installed the GitHub App. '
                    'Linked via GitHub account ID or username on installation event.',
    )
    account_name = models.CharField(max_length=255, help_text='GitHub org or user the app is installed on')
    account_type = models.CharField(max_length=50, blank=True, help_text='Organization or User')
    installation_id = models.BigIntegerField(unique=True)
    github_account_id = models.BigIntegerField(null=True, blank=True)
    sender_github_id = models.BigIntegerField(null=True, blank=True, help_text='GitHub ID of the user who installed the app')
    sender_login = models.CharField(max_length=255, blank=True, help_text='GitHub username of the installer')
    repository_selection = models.CharField(max_length=50, default='all', help_text='all or selected')
    access_metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account_name', 'installation_id']

    def __str__(self):
        return f'{self.account_name} ({self.installation_id})'

    @property
    def repos(self):
        return self.repositories.all()


class InstallationRepository(models.Model):
    """Tracks which repos an installation has access to."""

    installation = models.ForeignKey(
        GitHubInstallation,
        on_delete=models.CASCADE,
        related_name='repositories',
    )
    repo_id = models.BigIntegerField()
    repo_name = models.CharField(max_length=255)
    full_name = models.CharField(max_length=511)  # e.g. "acme/my-project"
    private = models.BooleanField(default=True)
    default_branch = models.CharField(max_length=100, default='main')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['full_name']
        unique_together = ['installation', 'repo_id']

    def __str__(self):
        return self.full_name


class FigmaConnection(models.Model):
    """Stores a user's Figma OAuth credentials for design token extraction."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='figma_connections',
    )
    access_token = models.TextField(help_text='Encrypted in production via pgcrypto')
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    figma_user_id = models.CharField(max_length=100, blank=True)
    figma_email = models.EmailField(blank=True)
    figma_username = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.figma_email or self.figma_username or self.figma_user_id} ({self.user_id})'

    @property
    def is_connected(self) -> bool:
        """Check if the connection has a non-empty access token."""
        return bool(self.access_token)

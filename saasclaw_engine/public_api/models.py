"""Public API key model for external access to the paycheck calculator API."""
import time
import hashlib
import secrets
import string

from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()


def generate_api_key(prefix='sk_'):
    """Generate a random API key with a recognizable prefix."""
    alphabet = string.ascii_letters + string.digits
    key = prefix + ''.join(secrets.choice(alphabet) for _ in range(40))
    return key


class ApiKey(models.Model):
    """API key for authenticating requests to the public calculator API."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_keys')
    name = models.CharField(max_length=100, help_text='Human-readable label for this key')
    prefix = models.CharField(max_length=10, db_index=True, help_text='First few chars for identification')
    key_hash = models.CharField(max_length=128, help_text='SHA-256 hash of the full key')
    usage_limit = models.PositiveIntegerField(null=True, blank=True, help_text='Max requests allowed; null = unlimited')
    usage_count = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.prefix}...)'

    @staticmethod
    def hash_key(key):
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def verify_key(key):
        """Look up an ApiKey by verifying its hash. Returns (apikey, user) or (None, None)."""
        key_hash = ApiKey.hash_key(key)
        try:
            apikey = ApiKey.objects.select_related('user').get(key_hash=key_hash, active=True)
            return apikey, apikey.user
        except ApiKey.DoesNotExist:
            return None, None

    @classmethod
    def create_key(cls, user, name, usage_limit=None):
        """Create a new API key. Returns (apikey_object, raw_key)."""
        raw_key = generate_api_key()
        key_hash = cls.hash_key(raw_key)
        prefix = raw_key[:7]
        apikey = cls.objects.create(
            user=user,
            name=name,
            prefix=prefix,
            key_hash=key_hash,
            usage_limit=usage_limit,
        )
        return apikey, raw_key



class HubSpotConnection(models.Model):
    """Per-project HubSpot OAuth connection for MCP bridge."""
    project = models.OneToOneField(
        'projects.Project',
        on_delete=models.CASCADE,
        related_name='hubspot_connection'
    )
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.BigIntegerField(help_text='Unix timestamp when access_token expires')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'HubSpot -> {self.project.slug}'

    @property
    def is_expired(self):
        import time as _time
        return _time.time() > self.expires_at - 300


class HubspotCompany(models.Model):
    """Cached HubSpot company record."""
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='hubspot_companies')
    hubspot_id = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=500)
    domain = models.CharField(max_length=500, blank=True, default='')
    industry = models.CharField(max_length=500, blank=True, default='')
    lifecycle_stage = models.CharField(max_length=100, blank=True, default='')
    last_updated = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'hubspot_id')
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.hubspot_id})'


class HubspotContact(models.Model):
    """Cached HubSpot contact record."""
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='hubspot_contacts')
    hubspot_id = models.CharField(max_length=50, db_index=True)
    first_name = models.CharField(max_length=200, blank=True, default='')
    last_name = models.CharField(max_length=200, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    company_name = models.CharField(max_length=500, blank=True, default='')
    company = models.ForeignKey(HubspotCompany, on_delete=models.SET_NULL, null=True, blank=True, related_name='contacts')
    lifecycle_stage = models.CharField(max_length=100, blank=True, default='')
    last_activity = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'hubspot_id')
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f'{self.first_name} {self.last_name} ({self.email})'


class TicketTopic(models.Model):
    """Auto-generated topic cluster from ticket data."""
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='ticket_topics')
    name = models.CharField(max_length=100)
    keywords = models.JSONField(default=list, blank=True)
    description = models.CharField(max_length=500, blank=True, default='')
    ticket_count = models.IntegerField(default=0)
    open_count = models.IntegerField(default=0)
    resolved_count = models.IntegerField(default=0)
    positive_count = models.IntegerField(default=0)
    negative_count = models.IntegerField(default=0)
    avg_sentiment_score = models.FloatField(default=0)
    companies = models.JSONField(default=list, blank=True)
    suggested_response = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'name')
        ordering = ['-ticket_count']

    def __str__(self):
        return f'{self.name} ({self.ticket_count} tickets)'


class HubspotTicket(models.Model):
    """Cached HubSpot ticket record with sentiment."""
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='hubspot_tickets')
    hubspot_id = models.CharField(max_length=50, db_index=True)
    subject = models.CharField(max_length=500)
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=100, blank=True, default='')
    pipeline_stage = models.CharField(max_length=50, blank=True, default='')
    company = models.ForeignKey(HubspotCompany, on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets')
    sentiment = models.CharField(max_length=20, default='neutral')
    sentiment_score = models.IntegerField(default=0)
    sentiment_summary = models.CharField(max_length=500, blank=True, default='')
    sentiment_flags = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    ai_summary = models.CharField(max_length=1000, blank=True, default='')
    topic = models.ForeignKey('TicketTopic', on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets')
    created_at_hubspot = models.DateTimeField(null=True, blank=True)
    last_updated_hubspot = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'hubspot_id')
        ordering = ['-last_updated_hubspot']

    def __str__(self):
        return f'{self.subject} ({self.hubspot_id})'

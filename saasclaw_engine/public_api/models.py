"""Public API key model for external access to the paycheck calculator API."""
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

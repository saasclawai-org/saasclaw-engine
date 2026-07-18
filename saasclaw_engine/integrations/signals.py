"""Signals for the integrations app.

Auto-provisions a Penpot account when a new user signs up on SaaSClaw.
"""
import logging

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)
User = get_user_model()


@receiver(post_save, sender=User, dispatch_uid="auto_provision_penpot")
def auto_provision_penpot(sender, instance, created, **kwargs):
    """When a new user is created, auto-provision a Penpot account."""
    if not created:
        return

    # Avoid circular import
    from .penpot_views import _provision_penpot_account
    from .models import PenpotConnection

    # Skip if already has a connection
    if PenpotConnection.objects.filter(user=instance).exists():
        return

    try:
        conn = _provision_penpot_account(instance)
        logger.info("Auto-provisioned Penpot account for new user %s", instance.username)
    except Exception as e:
        logger.error("Auto-provision Penpot failed for %s: %s", instance.username, e)
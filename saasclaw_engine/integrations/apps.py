from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'saasclaw_engine.integrations'

    def ready(self):
        # Connect the Penpot auto-provision signal
        from . import signals  # noqa: F401
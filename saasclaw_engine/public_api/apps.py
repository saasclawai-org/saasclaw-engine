from django.apps import AppConfig


class PublicApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'saasclaw_engine.public_api'
    verbose_name = 'Public API'
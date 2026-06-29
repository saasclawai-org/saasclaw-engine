from django.contrib import admin

from .models import ProviderKey, SiteSettings


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ['pk', 'project_approval_required', 'secret_scan_enabled', 'default_require_gateway', 'updated_at']

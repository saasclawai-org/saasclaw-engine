from django.contrib import admin

from .models import ProviderKey, SiteSettings, CustomPiiPattern


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ['pk', 'project_approval_required', 'secret_scan_enabled', 'default_require_gateway', 'updated_at']


@admin.register(CustomPiiPattern)
class CustomPiiPatternAdmin(admin.ModelAdmin):
    list_display = ['name', 'placeholder', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'regex']

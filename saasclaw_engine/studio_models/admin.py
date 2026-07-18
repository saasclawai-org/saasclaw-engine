from django.contrib import admin

from .models import ProviderKey, SiteSettings, CustomPiiPattern, TrainingModule, TrainingQuestion, TrainingCompletion


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ['pk', 'project_approval_required', 'openclaw_wizard_enabled', 'wizard_web_search_enabled', 'pii_guard_enabled', 'updated_at']
    fieldsets = (
        ('Project Management', {
            'fields': ('project_approval_required', 'enabled_frameworks', 'custom_domains_enabled'),
        }),
        ('Security', {
            'fields': ('secret_scan_enabled', 'dependency_scan_enabled', 'block_deploy_on_findings', 'pii_guard_enabled'),
        }),
        ('Wizard / AI', {
            'fields': ('openclaw_wizard_enabled', 'wizard_web_search_enabled', 'default_require_gateway', 'ai_disclosure_required'),
        }),
        ('Training', {
            'fields': ('require_training_before_project',),
        }),
        ('Integrations', {
            'fields': ('figma_enabled', 'allow_personal_keys'),
        }),
    )


@admin.register(CustomPiiPattern)
class CustomPiiPatternAdmin(admin.ModelAdmin):
    list_display = ['name', 'placeholder', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'regex']


class TrainingQuestionInline(admin.TabularInline):
    model = TrainingQuestion
    extra = 3
    ordering = ['order']


@admin.register(TrainingModule)
class TrainingModuleAdmin(admin.ModelAdmin):
    list_display = ['title', 'order', 'is_required', 'is_published', 'pass_threshold', 'created_at']
    list_filter = ['is_required', 'is_published']
    search_fields = ['title', 'description', 'content']
    prepopulated_fields = {'slug': ['title']}
    inlines = [TrainingQuestionInline]


@admin.register(TrainingCompletion)
class TrainingCompletionAdmin(admin.ModelAdmin):
    list_display = ['user', 'module', 'score', 'passed', 'completed_at']
    list_filter = ['passed', 'module']
    search_fields = ['user__username', 'module__title']
    readonly_fields = ['completed_at', 'answers']

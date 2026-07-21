from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'status', 'framework', 'linked_project', 'owner', 'last_deployed_at')
    list_filter = ('status', 'framework', 'linked_project_role')
    search_fields = ('name', 'slug', 'repo_url')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('owner', 'name', 'slug', 'description', 'status', 'framework', 'template_key')
        }),
        ('Linked Project', {
            'fields': ('linked_project', 'linked_project_role'),
            'classes': ('collapse',),
            'description': 'Pair a frontend with its backend. The wizard will include the linked project\'s API surface in its context.'
        }),
        ('Repository', {
            'fields': ('repo_provider', 'repo_owner', 'repo_name', 'repo_url', 'repo_default_branch'),
            'classes': ('collapse',)
        }),
        ('Deploy', {
            'fields': ('workspace_root', 'preview_domain', 'production_domain', 'last_deployed_at'),
            'classes': ('collapse',)
        }),
        ('Agent', {
            'fields': ('notes', 'directives', 'context_cache', 'require_gateway', 'risk_tier', 'onboarding_step'),
            'classes': ('collapse',)
        }),
    )

from django.contrib import admin
from .models import ProjectSubmission


@admin.register(ProjectSubmission)
class ProjectSubmissionAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "requester", "status", "data_sensitivity", "created_at")
    list_filter = ("status", "data_sensitivity", "created_at")
    search_fields = ("name", "slug", "requester__username", "description")
    readonly_fields = ("requester", "created_at", "updated_at")
    fieldsets = (
        ("Request", {
            "fields": ("requester", "name", "slug", "description", "framework", "source", "template", "repo_url")
        }),
        ("Context", {
            "fields": ("business_justification", "data_sensitivity", "estimated_timeline")
        }),
        ("Review", {
            "fields": ("status", "reviewer", "staff_notes", "require_gateway", "reviewed_at", "approved_project")
        }),
    )

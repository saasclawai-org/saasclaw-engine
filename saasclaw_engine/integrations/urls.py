"""URL patterns for Penpot integration API endpoints."""
from django.urls import path

from .penpot_views import (
    penpot_status,
    penpot_provision,
    penpot_projects,
    penpot_files,
    penpot_import,
)

urlpatterns = [
    path('penpot/status/', penpot_status, name='penpot_status'),
    path('penpot/provision/', penpot_provision, name='penpot_provision'),
    path('penpot/projects/', penpot_projects, name='penpot_projects'),
    path('penpot/files/<str:project_id>/', penpot_files, name='penpot_files'),
    path('penpot/import/<str:file_id>/', penpot_import, name='penpot_import'),
]
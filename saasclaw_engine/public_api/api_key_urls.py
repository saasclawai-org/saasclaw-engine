"""API key management URL configuration — mounted at /api/admin/."""
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from .api_key_views import api_keys_list_create, api_key_revoke, api_key_delete

urlpatterns = [
    path('keys/', csrf_exempt(api_keys_list_create), name='admin-api-keys-list-create'),
    path('keys/<str:key_id>/revoke/', csrf_exempt(api_key_revoke), name='admin-api-key-revoke'),
    path('keys/<str:key_id>/', csrf_exempt(api_key_delete), name='admin-api-key-delete'),
]

"""Public API URL configuration."""
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import views
from .api_key_views import api_keys_list_create, api_key_revoke, api_key_delete

urlpatterns = [
    # Auth
    path('auth/login/', csrf_exempt(views.login_view), name='public-api-login'),
    path('auth/token/', csrf_exempt(views.login_view), name='public-api-token'),  # alias for SimpleJWT-style clients
    path('auth/register/', csrf_exempt(views.register_view), name='public-api-register'),
    path('auth/exchange-session-token/', csrf_exempt(views.exchange_session_token), name='public-api-exchange-session-token'),
    path('auth/google/', csrf_exempt(views.google_auth), name='public-api-google-auth'),
    path('auth/github/', csrf_exempt(views.github_auth), name='public-api-github-auth'),
    path('auth/refresh/', csrf_exempt(views.token_refresh), name='public-api-token-refresh'),
    path('auth/github/redirect/', csrf_exempt(views.github_redirect), name='public-api-github-redirect'),

    # Projects
    path('projects/', csrf_exempt(views.projects_list_create), name='public-api-projects'),
    path('projects/<str:slug>/', csrf_exempt(views.project_detail), name='public-api-project-detail'),
    path('projects/<str:slug>/link/', csrf_exempt(views.link_project), name='public-api-project-link'),
    path('projects/<str:slug>/unlink/', csrf_exempt(views.unlink_project), name='public-api-project-unlink'),

    # Files
    path('projects/<str:slug>/files/', csrf_exempt(views.files_list), name='public-api-files-list'),
    path('projects/<str:slug>/files/<path:path>', csrf_exempt(views.file_detail), name='public-api-file-detail'),

    # Sessions (chat)
    path('projects/<str:slug>/sessions/', csrf_exempt(views.sessions_list_create), name='public-api-sessions'),
    path('projects/<str:slug>/sessions/<uuid:session_id>/', csrf_exempt(views.session_detail), name='public-api-session-detail'),
    path('projects/<slug:project_slug>/sessions/<uuid:session_id>/reset/', csrf_exempt(views.session_reset), name='public-api-session-reset'),
    path('projects/<str:slug>/sessions/<uuid:session_id>/send/', csrf_exempt(views.session_send), name='public-api-session-send'),

    # Env vars
    path('projects/<str:slug>/env/', csrf_exempt(views.env_list_create), name='public-api-env-list'),
    path('projects/<str:slug>/env/<str:key>/', csrf_exempt(views.env_delete), name='public-api-env-delete'),

    # Deploy
    path('projects/<str:slug>/deploy/', csrf_exempt(views.deploy_trigger), name='public-api-deploy'),
    path('projects/<str:slug>/deploy/status/', csrf_exempt(views.deploy_status), name='public-api-deploy-status'),
    path('projects/<str:slug>/deploy/history/', csrf_exempt(views.deploy_history), name='public-api-deploy-history'),

    # Git
    path('projects/<str:slug>/git/status/', csrf_exempt(views.git_status_view), name='public-api-git-status'),
    path('projects/<str:slug>/git/diff/', csrf_exempt(views.git_diff_view), name='public-api-git-diff'),
    path('projects/<str:slug>/git/commit/', csrf_exempt(views.git_commit_view), name='public-api-git-commit'),

    # Infrastructure
    path('projects/<str:slug>/status/', csrf_exempt(views.project_status), name='public-api-project-status'),
    path('projects/<str:slug>/logs/<str:source>/', csrf_exempt(views.logs_view), name='public-api-logs'),

    # API Keys
    path('admin/keys/', csrf_exempt(api_keys_list_create), name='public-api-keys-list-create'),
    path('admin/keys/<str:key_id>/revoke/', csrf_exempt(api_key_revoke), name='public-api-key-revoke'),
    path('admin/keys/<str:key_id>/', csrf_exempt(api_key_delete), name='public-api-key-delete'),

    # Account / Profile
    path('account/', csrf_exempt(views.account_profile), name='public-api-account-profile'),
    path('account/provider-keys/', csrf_exempt(views.provider_keys_list_create), name='public-api-provider-keys'),
    path('account/provider-keys/<str:key_id>/', csrf_exempt(views.provider_key_delete), name='public-api-provider-key-delete'),
]
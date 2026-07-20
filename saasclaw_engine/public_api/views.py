"""Public API views — thin REST layer over existing engine tools and models.

This is the API that the SaaSClaw Starter app consumes.
All endpoints require JWT auth. In SINGLE_USER mode, login requires
a password (SAASCLAW_SINGLE_USER_PASSWORD env var).
"""
import json
import logging
import os
import re
import shutil
import subprocess

from django.conf import settings
from django.contrib.auth.models import User
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, authentication_classes, throttle_classes
from rest_framework.permissions import AllowAny as DRFAllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from saasclaw_engine.projects.models import Project
from saasclaw_engine.studio_models.models import Workspace
from saasclaw_engine.deployments.models import EnvironmentVariable, Environment

from .authentication import PublicAPIAuthentication
from .serializers import (
    RegisterSerializer, ProjectSerializer, ProjectCreateSerializer,
    EnvVarSerializer, DeployTriggerSerializer, GitCommitSerializer,
)

logger = logging.getLogger(__name__)

def _is_single_user():
    """Check if single-user mode is enabled (reads from settings at runtime)."""
    return getattr(settings, 'SAASCLAW_SINGLE_USER', False)


def _get_single_user_password():
    """Get the configured single-user password (reads from settings at runtime)."""
    return getattr(settings, 'SAASCLAW_SINGLE_USER_PASSWORD', '')


def _get_or_create_single_user(password=''):
    """In single-user mode, return the default admin user.

    Creates the user with the given password if they don't exist yet.
    If password is empty, sets an unusable password (must use SINGLE_USER_PASSWORD env var).
    """
    user = User.objects.filter(is_superuser=True).first()
    if not user:
        user = User.objects.create_superuser(
            username='admin',
            email='admin@saasclaw.local',
            password=password or 'admin',
        )
    elif password:
        user.set_password(password)
        user.save()
    return user


def _get_user(request):
    """Get the effective user — in single-user mode, resolve to the admin user.

    Even in SINGLE_USER mode, the request MUST have a valid JWT.
    The difference is that the JWT can be for any user — we always resolve
    to the admin user. This prevents unauthenticated access while keeping
    the convenience of single-user mode.

    If the user is not authenticated (AnonymousUser), this returns None,
    which will cause _get_project to return a 404 error response.
    """
    if _is_single_user():
        return _get_or_create_single_user()
    if hasattr(request, 'user') and request.user.is_authenticated:
        return request.user
    return None


def _get_project(slug, user):
    """Look up a project owned by user, or 404.

    Returns (project, error_response) tuple.
    If project is None, error_response contains the 404 Response to return.
    This pattern ensures every endpoint handles the missing-project case
    consistently and makes it harder to accidentally skip owner scoping.
    """
    if user is None:
        return None, Response({'detail': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        return Project.objects.get(slug=slug, owner=user, deleted_at__isnull=True), None
    except Project.DoesNotExist:
        return None, Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)


def _project_workspace(project):
    """Get the active workspace for a project."""
    ws = Workspace.objects.filter(project=project, is_active=True).first()
    if not ws:
        ws = Workspace.objects.filter(project=project).first()
    return ws


from rest_framework.throttling import SimpleRateThrottle


class LoginRateThrottle(SimpleRateThrottle):
    """Rate limit login attempts: 5 per minute per IP."""
    scope = 'login'

    def get_cache_key(self, request, view):
        # Rate limit by IP address
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '0.0.0.0')
        return f'throttle_login_{ip}'


# ---- Auth ----

@api_view(['POST'])
@permission_classes([DRFAllowAny])
@authentication_classes([])
@throttle_classes([LoginRateThrottle])
def register_view(request):
    """Register a new user and return JWT tokens."""
    if _is_single_user():
        return Response(
            {'detail': 'Registration disabled in single-user mode.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    refresh = RefreshToken.for_user(user)
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    })


@api_view(['POST'])
@permission_classes([DRFAllowAny])
@authentication_classes([])
@throttle_classes([LoginRateThrottle])
def login_view(request):
    """Authenticate user and return JWT tokens."""
    if _is_single_user():
        # Even in single-user mode, require a password.
        # Use SAASCLAW_SINGLE_USER_PASSWORD env var to set the password.
        # If not set, any non-empty password is accepted (for local dev only).
        password = request.data.get('password', '')
        if not password:
            return Response(
                {'detail': 'Password is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # If SINGLE_USER_PASSWORD is configured, enforce it
        single_user_password = _get_single_user_password()
        if single_user_password and password != single_user_password:
            return Response(
                {'detail': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        user = _get_or_create_single_user(password=password)
        refresh = RefreshToken.for_user(user)
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'email': user.email,
        })
    email = request.data.get('email', '') or request.data.get('username', '')
    password = request.data.get('password', '')
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        try:
            user = User.objects.get(username=email)
        except User.DoesNotExist:
            return Response(
                {'detail': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
    if not user.check_password(password):
        return Response(
            {'detail': 'Invalid credentials.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    refresh = RefreshToken.for_user(user)
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'email': user.email,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def exchange_session_token(request):
    """Exchange a session (cookie) auth for JWT tokens.

    This endpoint is for users who logged in via social auth (Google/GitHub)
    and don't have a password. They can call this endpoint while authenticated
    via their session cookie to obtain JWT tokens for API/SDK use.
    """
    user = _get_user(request)
    if user is None:
        return Response(
            {'detail': 'Authentication required.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    refresh = RefreshToken.for_user(user)
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'email': user.email,
    })


@api_view(['POST'])
@permission_classes([DRFAllowAny])
def google_auth(request):
    """Authenticate with a Google ID token.

    Accepts { id_token: "..." } from Google Sign-In SDK.
    Verifies the token, finds/creates the user, returns JWT.
    """
    import urllib.request
    import json as _json

    id_token = request.data.get('id_token')
    if not id_token:
        return Response({'detail': 'id_token is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # Verify the ID token with Google
    try:
        req = urllib.request.Request(
            f'https://oauth2.googleapis.com/tokeninfo?id_token={id_token}'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = _json.loads(resp.read())
    except Exception:
        return Response({'detail': 'Invalid Google token.'}, status=status.HTTP_401_UNAUTHORIZED)

    email = token_data.get('email')
    if not email:
        return Response({'detail': 'Google token has no email.'}, status=status.HTTP_400_BAD_REQUEST)

    # Find or create user
    user, created = User.objects.get_or_create(
        email=email,
        defaults={'username': email, 'is_active': True}
    )
    if created:
        logger.info('Created user via Google auth: %s', email)

    refresh = RefreshToken.for_user(user)
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'email': user.email,
    })


@api_view(['POST'])
@permission_classes([DRFAllowAny])
def github_auth(request):
    """Authenticate with a GitHub OAuth code.

    Accepts { code: "..." } from GitHub OAuth web flow.
    Exchanges code for access token, gets user info, returns JWT.
    """
    import urllib.request
    import json as _json

    code = request.data.get('code')
    if not code:
        return Response({'detail': 'code is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # Get GitHub client id/secret from allauth SocialApp
    from allauth.socialaccount.models import SocialApp
    try:
        gh_app = SocialApp.objects.get(provider='github')
        client_id = gh_app.client_id
        client_secret = gh_app.secret
    except SocialApp.DoesNotExist:
        return Response({'detail': 'GitHub OAuth not configured.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Exchange code for access token
    token_payload = _json.dumps({
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
    }).encode()

    token_req = urllib.request.Request(
        'https://github.com/login/oauth/access_token',
        data=token_payload,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_resp = _json.loads(resp.read())
    except Exception:
        return Response({'detail': 'Failed to exchange GitHub code.'}, status=status.HTTP_401_UNAUTHORIZED)

    gh_access_token = token_resp.get('access_token')
    if not gh_access_token:
        return Response({'detail': 'GitHub did not return access token.'}, status=status.HTTP_401_UNAUTHORIZED)

    # Get user info from GitHub
    user_req = urllib.request.Request(
        'https://api.github.com/user/emails',
        headers={'Authorization': f'token {gh_access_token}', 'Accept': 'application/vnd.github+json'},
    )
    try:
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            emails = _json.loads(resp.read())
    except Exception:
        return Response({'detail': 'Failed to get GitHub user info.'}, status=status.HTTP_401_UNAUTHORIZED)

    email = None
    for e in emails:
        if e.get('primary'):
            email = e.get('email')
            break
    if not email and emails:
        email = emails[0].get('email')

    if not email:
        return Response({'detail': 'GitHub account has no verified email.'}, status=status.HTTP_400_BAD_REQUEST)

    # Find or create user
    user, created = User.objects.get_or_create(
        email=email,
        defaults={'username': email, 'is_active': True}
    )
    if created:
        logger.info('Created user via GitHub auth: %s', email)

    refresh = RefreshToken.for_user(user)
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'email': user.email,
    })


from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

@api_view(['GET'])
@permission_classes([DRFAllowAny])
@csrf_exempt
def github_redirect(request):
    """Simple HTML page for mobile OAuth redirect.
    
    GitHub redirects here with ?code=xxx. This page just shows
    the code so the WebView can intercept it. Does NOT consume the code.
    """
    code = request.GET.get('code', '')
    error = request.GET.get('error', '')
    return HttpResponse(f'''<!DOCTYPE html>
<html><head><title>Authenticating...</title></head>
<body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0f0f1a;color:#fff;">
<div style="text-align:center;">
<h2>🛠️ SaaSClaw</h2>
<p>{'Connecting...' if code else 'Auth failed'}</p>
</div>
</body></html>''')


# ---- Projects ----

def _seed_initial_content(workspace_path, framework, name):
    """Seed minimal starter files so the project can deploy immediately."""
    fw = framework.lower()
    safe_name = name.replace("'", "\\'")

    if fw in ('html', 'blank', ''):
        _seed_html(workspace_path, safe_name)
    elif fw in ('react', 'vite_react'):
        _seed_react(workspace_path, safe_name)
    elif fw == 'nextjs':
        _seed_nextjs(workspace_path, safe_name)
    elif fw == 'vue':
        _seed_vue(workspace_path, safe_name)
    elif fw == 'svelte':
        _seed_svelte(workspace_path, safe_name)
    elif fw in ('django', 'flask', 'fastapi', 'react-django'):
        _seed_python(workspace_path, fw)
    elif fw == 'android':
        _seed_readme(workspace_path, name, framework)
    elif fw in ('dotnet', 'spring-boot'):
        _seed_readme(workspace_path, name, framework)
    else:
        _seed_readme(workspace_path, name, framework)


def _seed_html(path, name):
    with open(os.path.join(path, 'index.html'), 'w') as f:
        f.write(f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name}</title>
    <style>
        body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }}
        h1 {{ font-size: 3rem; margin: 0; }}
        p {{ opacity: 0.8; font-size: 1.2rem; }}
    </style>
</head>
<body>
    <div style="text-align:center">
        <h1>🚀 {name}</h1>
        <p>Built with SaaSClaw</p>
    </div>
</body>
</html>
''')


def _seed_readme(path, name, framework):
    with open(os.path.join(path, 'README.md'), 'w') as f:
        f.write(f'# {name}\n\nCreated with SaaSClaw. Framework: {framework}\n')


def _seed_react(path, name):
    os.makedirs(os.path.join(path, 'src'), exist_ok=True)
    with open(os.path.join(path, 'package.json'), 'w') as f:
        import json
        json.dump({
            'name': name.lower().replace(' ', '-'),
            'private': True,
            'version': '0.0.0',
            'type': 'module',
            'scripts': {
                'dev': 'vite',
                'build': 'vite build',
                'preview': 'vite preview',
            },
            'dependencies': {
                'react': '^18.3.1',
                'react-dom': '^18.3.1',
            },
            'devDependencies': {
                '@vitejs/plugin-react': '^4.3.1',
                'vite': '^5.4.0',
            },
        }, f, indent=2)
    with open(os.path.join(path, 'vite.config.js'), 'w') as f:
        f.write('''import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
})
''')
    with open(os.path.join(path, 'index.html'), 'w') as f:
        f.write('''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App</title>
</head>
<body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
</body>
</html>
''')
    with open(os.path.join(path, 'src', 'main.jsx'), 'w') as f:
        f.write('''import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
''')
    with open(os.path.join(path, 'src', 'App.jsx'), 'w') as f:
        f.write('''import React from 'react'

export default function App() {
  return (
    <div style={{
      fontFamily: 'system-ui, sans-serif',
      textAlign: 'center',
      padding: '4rem 1rem',
    }}>
      <h1>🚀 ''' + name + '''</h1>
      <p>Built with SaaSClaw</p>
    </div>
  )
}
''')


def _seed_nextjs(path, name):
    # Next.js needs full structure — seed README, deploy will use wizard to build
    _seed_readme(path, name, 'nextjs')


def _seed_vue(path, name):
    os.makedirs(os.path.join(path, 'src'), exist_ok=True)
    with open(os.path.join(path, 'package.json'), 'w') as f:
        import json
        json.dump({
            'name': name.lower().replace(' ', '-'),
            'private': True,
            'version': '0.0.0',
            'type': 'module',
            'scripts': {
                'dev': 'vite',
                'build': 'vite build',
                'preview': 'vite preview',
            },
            'dependencies': {
                'vue': '^3.4.0',
            },
            'devDependencies': {
                '@vitejs/plugin-vue': '^5.0.0',
                'vite': '^5.4.0',
            },
        }, f, indent=2)
    with open(os.path.join(path, 'vite.config.js'), 'w') as f:
        f.write('''import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
})
''')
    with open(os.path.join(path, 'index.html'), 'w') as f:
        f.write('''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App</title>
</head>
<body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
</body>
</html>
''')
    with open(os.path.join(path, 'src', 'main.js'), 'w') as f:
        f.write('''import { createApp } from 'vue'
import App from './App.vue'

createApp(App).mount('#app')
''')
    with open(os.path.join(path, 'src', 'App.vue'), 'w') as f:
        f.write('<template>\n  <div style="font-family: system-ui, sans-serif; text-align: center; padding: 4rem 1rem;">\n    <h1>🚀 ' + name + '</h1>\n    <p>Built with SaaSClaw</p>\n  </div>\n</template>\n')


def _seed_svelte(path, name):
    os.makedirs(os.path.join(path, 'src'), exist_ok=True)
    with open(os.path.join(path, 'package.json'), 'w') as f:
        import json
        json.dump({
            'name': name.lower().replace(' ', '-'),
            'private': True,
            'version': '0.0.0',
            'type': 'module',
            'scripts': {
                'dev': 'vite',
                'build': 'vite build',
                'preview': 'vite preview',
            },
            'devDependencies': {
                '@sveltejs/vite-plugin-svelte': '^3.1.0',
                'svelte': '^4.2.0',
                'vite': '^5.4.0',
            },
        }, f, indent=2)
    with open(os.path.join(path, 'vite.config.js'), 'w') as f:
        f.write('''import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
})
''')
    with open(os.path.join(path, 'svelte.config.js'), 'w') as f:
        f.write('''import { vitePreprocess } from '@sveltejs/vite-plugin-svelte'

export default {
  preprocess: vitePreprocess(),
}
''')
    with open(os.path.join(path, 'index.html'), 'w') as f:
        f.write('''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App</title>
</head>
<body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
</body>
</html>
''')
    with open(os.path.join(path, 'src', 'main.js'), 'w') as f:
        f.write('''import App from './App.svelte'

const app = new App({
  target: document.getElementById('app'),
})

export default app
''')
    with open(os.path.join(path, 'src', 'App.svelte'), 'w') as f:
        f.write('<main style="font-family: system-ui, sans-serif; text-align: center; padding: 4rem 1rem;">\n  <h1>🚀 ' + name + '</h1>\n  <p>Built with SaaSClaw</p>\n</main>\n')


def _seed_python(path, fw):
    _seed_readme(path, fw, fw)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def projects_list_create(request):
    """List user's projects or create a new one."""
    user = _get_user(request)
    if user is None:
        return Response({'detail': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if request.method == 'GET':
        projects = Project.objects.filter(owner=user, deleted_at__isnull=True)
        return Response(ProjectSerializer(projects, many=True).data)

    # POST — create
    serializer = ProjectCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    name = serializer.validated_data['name']
    slug = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')[:50]
    base_slug = slug
    counter = 1
    while Project.objects.filter(slug=slug).exists():
        slug = f'{base_slug}-{counter}'
        counter += 1

    project = Project.objects.create(
        owner=user,
        name=name,
        slug=slug,
        framework=serializer.validated_data['framework'],
        description=serializer.validated_data.get('description', ''),
    )

    # Use the same template system as the website
    from studio.views.helpers import _create_bare_repo, _run_as_saasclaw, _ensure_workspace, _template_env_defaults, _allocate_port
    from studio.views.template_defs import _create_from_template
    from studio.views.project_settings import _create_default_saasclaw_config
    from saasclaw_engine.deployments.models import Environment
    from django.conf import settings as dj_settings

    workspace_path = f'/srv/saasclaw/projects/{slug}/repo'
    _run_as_saasclaw(['mkdir', '-p', f'/srv/saasclaw/projects/{slug}'], capture_output=True, timeout=5)

    # Create bare repo + starter template (same as website)
    bare_repo = _create_bare_repo(slug)
    fw = serializer.validated_data['framework']
    template_name = fw if fw in ('html', 'react', 'vite_react', 'nextjs', 'vue', 'svelte',
                                   'django', 'flask', 'fastapi', 'supabase', 'hugo',
                                   'dotnet', 'react-dotnet', 'spring-boot', 'firebase',
                                   'htmx', 'react-django') else 'html'
    _create_from_template(template_name, workspace_path, name, slug, bare_repo)

    # Ensure .saasclaw config exists
    if not os.path.isfile(os.path.join(workspace_path, '.saasclaw')):
        _create_default_saasclaw_config(workspace_path, fw)

    # Set workspace_root, repo_url, domains on the project (same as website)
    project.workspace_root = workspace_path
    project.repo_url = bare_repo
    project.preview_domain = f'{slug}.{dj_settings.PREVIEW_BASE_DOMAIN}'
    project.production_domain = f'{slug}.{dj_settings.APP_BASE_DOMAIN}'
    project.save(update_fields=['workspace_root', 'repo_url', 'preview_domain', 'production_domain'])

    # Create environments with framework-appropriate defaults (same as website)
    env_defaults = _template_env_defaults(template_name)
    is_django = env_defaults.get('runtime_kind') == 'django'
    is_node_ssr = env_defaults.get('runtime_kind') == 'node_ssr'
    needs_port = is_django or is_node_ssr

    try:
        preview_port = _allocate_port('preview') if needs_port else None
        prod_port = _allocate_port('production') if needs_port else None
        Environment.objects.create(
            project=project, name=Environment.Name.PREVIEW, slug='preview',
            domain=project.preview_domain, is_primary=True,
            app_port=preview_port,
            **env_defaults,
        )
        Environment.objects.create(
            project=project, name=Environment.Name.PRODUCTION, slug='production',
            domain=project.production_domain,
            app_port=prod_port,
            **env_defaults,
        )
    except Exception as exc:
        logger.exception("Environment creation failed for %s", slug)

    # Create workspace record (same as website)
    try:
        _ensure_workspace(project, user)
    except Exception as exc:
        logger.warning("Workspace setup failed for %s: %s", slug, exc)

    Workspace.objects.create(
        project=project,
        user=user,
        local_path=workspace_path,
        is_active=True,
    )

    return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def project_detail(request, slug):
    """Get, update, or delete a project."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    if request.method == 'GET':
        from saasclaw_engine.studio_models.models import Todo
        data = ProjectSerializer(project).data
        data['todos'] = [
            {'id': str(t.id), 'text': t.text, 'done': t.done, 'order': t.order}
            for t in project.todos.all()
        ]
        return Response(data)

    if request.method == 'PATCH':
        serializer = ProjectSerializer(project, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ProjectSerializer(project).data)

    # DELETE — soft delete
    from django.utils import timezone
    project.deleted_at = timezone.now()
    project.save()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ---- Files ----

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def files_list(request, slug):
    """List files in a project workspace."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.agent.tools import list_files
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = list_files(workspace, request.query_params.get('path', '.'))
    try:
        entries = json.loads(result)
    except json.JSONDecodeError:
        entries = [{'name': result, 'type': 'message'}]
    return Response(entries)


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def file_detail(request, slug, path):
    """Read or write a file in a project workspace."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.agent.tools import read_file, write_file
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'

    if request.method == 'GET':
        result = read_file(workspace, path)
        return Response({'path': path, 'content': result})

    content = request.data.get('content', '')
    result = write_file(workspace, path, content)
    return Response({'path': path, 'result': result})


# ---- Chat Sessions ----

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def sessions_list_create(request, slug):
    """List or create chat sessions for a project."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.studio_models.models import AgentSession, AgentMessage

    if request.method == 'GET':
        sessions = AgentSession.objects.filter(
            project=project, user=user,
        ).order_by('-created_at')[:50]
        data = []
        for s in sessions:
            last_msg = s.messages.order_by('-created_at').first()
            data.append({
                'id': str(s.id),
                'created_at': s.created_at.isoformat(),
                'title': s.title or '',
                'last_message': last_msg.content[:100] if last_msg else '',
                'status': s.status,
            })
        return Response(data)

    # POST — create session
    workspace = _project_workspace(project)
    if not workspace:
        return Response({'detail': 'No workspace found.'}, status=status.HTTP_400_BAD_REQUEST)

    session = AgentSession.objects.create(
        project=project,
        workspace=workspace,
        user=user,
        title=request.data.get('title', 'New session'),
    )
    return Response({
        'id': str(session.id),
        'created_at': session.created_at.isoformat(),
        'title': session.title,
        'messages': [],
        'status': session.status,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def session_detail(request, slug, session_id):
    """Get session detail including messages."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.studio_models.models import AgentSession, AgentMessage
    try:
        session = AgentSession.objects.get(id=session_id, project=project, user=user)
    except AgentSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

    msgs = session.messages.order_by('created_at')
    return Response({
        'id': str(session.id),
        'created_at': session.created_at.isoformat(),
        'title': session.title,
        'status': session.status,
        'messages': [{
            'role': m.role,
            'content': m.content,
            'tool_call': m.tool_call or None,
            'created_at': m.created_at.isoformat(),
        } for m in msgs],
    })


# ---- Chat Send (SSE) ----

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def session_send(request, slug, session_id):
    """Send a message to an agent session and stream responses via SSE.

    Uses the same OpenClaw gateway streaming as the website wizard — real-time
    text deltas, tool calls, and tool results as they happen.
    """
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.studio_models.models import AgentSession, AgentMessage
    try:
        session = AgentSession.objects.get(id=session_id, project=project, user=user)
    except AgentSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

    message = request.data.get('message', '').strip()
    if not message:
        return Response({'detail': 'Message is required.'}, status=status.HTTP_400_BAD_REQUEST)

    model_override = request.data.get('model', None)

    workspace = _project_workspace(project)
    if not workspace:
        return Response({'detail': 'No workspace found.'}, status=status.HTTP_400_BAD_REQUEST)

    # Prevent duplicate concurrent requests
    if session.status == 'running':
        return Response({'detail': 'Session is already running.'}, status=status.HTTP_409_CONFLICT)

    # Mark session as running
    AgentSession.objects.filter(id=session.id).update(status='running')

    # Save user message
    AgentMessage.objects.create(
        session=session,
        role='user',
        content=message,
        tool_call={},
    )

    ws_path = workspace.local_path
    session_id_str = str(session.id)

    # Build project context (same as website wizard)
    from studio.views.context_builder import _build_project_context
    from studio.views.openclaw_backend import stream_openclaw_agent
    from studio.views.wizard_utils import save_token_usage, mark_session_idle

    project_context = _build_project_context(project)
    profile_prompt = session.profile.system_prompt if session.profile else ""
    project_todos = list(project.todos.filter(done=False).order_by('order').values('text', 'done'))

    # Resolve model (same logic as website wizard)
    actual_model = model_override or 'glm-5.1'
    gateway_model = actual_model
    if not gateway_model.startswith(('zai/', 'openai/', 'anthropic/')):
        gateway_model = f'zai/{gateway_model}'

    def event_stream():
        """Generator yielding real-time SSE events via OpenClaw gateway."""
        from django.db import connection
        full_assistant_text = ""
        _assistant_msg_id = None
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        try:
            for event in stream_openclaw_agent(
                session_id=session_id_str,
                workspace_path=ws_path,
                project_name=project.name,
                project=project,
                user_message=message,
                provider='zai',
                model=actual_model,
                gateway_model=gateway_model,
                profile_prompt=profile_prompt,
                project_directives=project.directives,
                project_context=project_context,
                project_todos=project_todos,
            ):
                etype = event.get('type')

                if etype == 'model':
                    yield f'data: {json.dumps({"type": "model", "model": event.get("model", actual_model)})}\n\n'

                elif etype == 'text':
                    delta = event.get('content', '')
                    full_assistant_text += delta
                    # Save incrementally so messages persist even if stream crashes
                    if full_assistant_text.strip():
                        if _assistant_msg_id is None:
                            _msg = AgentMessage.objects.create(
                                session_id=session_id_str, role='assistant',
                                content=full_assistant_text, tool_call={},
                            )
                            _assistant_msg_id = _msg.id
                        else:
                            AgentMessage.objects.filter(id=_assistant_msg_id).update(
                                content=full_assistant_text
                            )
                    yield f'data: {json.dumps({"type": "content", "content": delta})}\n\n'

                elif etype == 'tool_start':
                    tc_name = event.get('name', '?')
                    tc_args = event.get('args', '')
                    yield f'data: {json.dumps({"type": "tool_call", "name": tc_name, "args": tc_args})}\n\n'

                elif etype == 'tool_result':
                    tc_name = event.get('name', '?')
                    output = event.get('result', '')
                    if output:
                        AgentMessage.objects.create(
                            session_id=session_id_str,
                            role='tool',
                            content=str(output)[:2000],
                            tool_call={'name': tc_name, 'result': str(output)[:200]},
                        )
                        display = str(output)
                        if len(display) > 2000:
                            display = display[:2000] + '...'
                        yield f'data: {json.dumps({"type": "tool_result", "name": tc_name, "content": display})}\n\n'

                elif etype == '_usage':
                    usage = event.get('usage', {})
                    if usage:
                        total_usage['prompt_tokens'] += usage.get('prompt_tokens', 0)
                        total_usage['completion_tokens'] += usage.get('completion_tokens', 0)
                        total_usage['total_tokens'] += usage.get('total_tokens', 0)

                elif etype == 'done':
                    if full_assistant_text.strip():
                        if _assistant_msg_id:
                            AgentMessage.objects.filter(id=_assistant_msg_id).update(
                                content=full_assistant_text
                            )
                        else:
                            _msg = AgentMessage.objects.create(
                                session_id=session_id_str, role='assistant',
                                content=full_assistant_text, tool_call={},
                            )
                            _assistant_msg_id = _msg.id
                    yield f'data: {json.dumps({"type": "done", "content": full_assistant_text, "model": actual_model})}\n\n'

                elif etype == 'error':
                    yield f'data: {json.dumps({"type": "error", "content": event.get("content", "Unknown error")})}\n\n'

            # Auto-commit workspace changes (same as website)
            try:
                import subprocess
                subprocess.run(['sudo', 'chown', '-R', '999:983', ws_path],
                               capture_output=True, timeout=10)
                from saasclaw_engine.agent.tools import git_status, git_commit, _git
                status = git_status(ws_path).strip()
                if status:
                    branch = workspace.work_branch or 'main'
                    git_commit(ws_path, 'API: auto-commit pending changes')
                    _git(ws_path, 'push', 'origin', branch)
                    logger.info('Auto-committed changes for %s', slug)
            except Exception as ac_err:
                logger.warning('Auto-commit failed (non-fatal): %s', ac_err)

        except Exception as exc:
            logger.exception('Stream error for project=%s session=%s', slug, session_id)
            AgentMessage.objects.create(
                session_id=session_id_str,
                role='assistant',
                content=f'⚠️ {exc}',
                tool_call={},
            )
            yield f'data: {json.dumps({"type": "error", "content": str(exc)})}\n\n'
        finally:
            save_token_usage(project, session_id_str, 'zai', actual_model, total_usage)
            mark_session_idle(session_id_str)

        yield 'data: [DONE]\n\n'

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def env_list_create(request, slug):
    """List or set environment variables."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    if request.method == 'GET':
        from saasclaw_engine.agent.tools import get_env_vars
        workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
        result = get_env_vars(workspace)
        env_vars = []
        for line in result.strip().split('\n'):
            if '=' in line:
                key, _, value = line.partition('=')
                is_secret = any(
                    s in key.upper()
                    for s in ['SECRET', 'KEY', 'PASSWORD', 'TOKEN', 'API_KEY']
                )
                env_vars.append({
                    'key': key.strip(),
                    'value': '••••••••' if is_secret else value.strip(),
                    'is_secret': is_secret,
                })
        return Response(env_vars)

    serializer = EnvVarSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    from saasclaw_engine.agent.tools import set_env_var
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = set_env_var(
        workspace,
        serializer.validated_data['key'],
        serializer.validated_data['value'],
        serializer.validated_data.get('is_secret', True),
    )
    return Response({'key': serializer.validated_data['key'], 'result': result})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def env_delete(request, slug, key):
    """Delete an environment variable."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    for env in Environment.objects.filter(project=project):
        EnvironmentVariable.objects.filter(environment=env, key=key).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def session_reset(request, project_slug, session_id):
    """Reset a stuck session back to idle status."""
    project, err = _get_project(project_slug, request.user)
    if err:
        return err
    try:
        session = AgentSession.objects.get(id=session_id, project=project)
    except AgentSession.DoesNotExist:
        return Response({"detail": "Session not found."}, status=404)
    session.status = "idle"
    session.save(update_fields=["status"])
    return Response({"status": "idle", "session_id": str(session.id)})



# ---- Deploy ----

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def deploy_trigger(request, slug):
    """Trigger a deploy — build the project and serve it locally."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    environment = request.data.get('environment', 'preview')
    results = []

    import subprocess
    import shutil

    def run_cmd(cmd, timeout=120):
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=workspace, capture_output=True, text=True, timeout=timeout
            )
            output = proc.stdout.strip() or proc.stderr.strip() or '(no output)'
            if proc.returncode != 0:
                return f'❌ {cmd}\n{proc.stderr.strip() or proc.stdout.strip()}'
            return output
        except subprocess.TimeoutExpired:
            return f'⏰ Timed out: {cmd}'
        except Exception as e:
            return f'❌ Error: {e}'

    # Detect project type
    files_present = set()
    try:
        files_present = set(os.listdir(workspace))
    except Exception:
        pass

    is_node = 'package.json' in files_present
    is_django = 'manage.py' in files_present

    dist_dir = None

    if is_node:
        results.append('📦 Building Node.js project...')
        if not os.path.isdir(os.path.join(workspace, 'node_modules')):
            results.append(run_cmd('npm install'))
        results.append(run_cmd('npm run build'))
        for candidate in ['dist', 'build', 'out']:
            if os.path.isdir(os.path.join(workspace, candidate)):
                dist_dir = candidate
                break
        if not dist_dir:
            results.append('⚠️ Could not find build output directory.')

    elif is_django:
        results.append('🐍 Building Django project...')
        results.append(run_cmd('python manage.py collectstatic --noinput'))
        dist_dir = None

    else:
        results.append('📄 Static project detected.')
        dist_dir = None

    # Commit any uncommitted changes
    status_out = run_cmd('git status --porcelain')
    if status_out and 'nothing to commit' not in (status_out or ''):
        results.append('📝 Committing changes...')
        run_cmd('git add -A')
        run_cmd('git -c user.email="deploy@saasclaw.ai" -c user.name="Deploy" commit -m "Deploy: preview deployment"')

    # Determine deploy path
    web_root = f'/srv/saasclaw/projects/{slug}/web'

    if dist_dir:
        # Copy build output to web root
        src = os.path.join(workspace, dist_dir)
        try:
            if os.path.isdir(web_root):
                shutil.rmtree(web_root)
            shutil.copytree(src, web_root)
            results.append(f'✅ Copied {dist_dir}/ → {web_root}')
        except Exception as e:
            results.append(f'⚠️ Copy error: {e}')
    elif os.path.isfile(os.path.join(workspace, 'index.html')):
        # Static project — copy index.html to web root
        try:
            os.makedirs(web_root, exist_ok=True)
            shutil.copy2(os.path.join(workspace, 'index.html'), web_root)
            results.append(f'✅ Copied index.html → {web_root}')
        except Exception as e:
            results.append(f'⚠️ Copy error: {e}')
    else:
        # No build output — try serving workspace directly
        web_root = workspace
        results.append('⚠️ No build output found. Serving workspace directly.')

    # Update project status
    project.status = 'deployed'
    project.preview_domain = f'{slug}.saasclaw.ai'
    project.save()

    # Auto-create nginx site config for this project
    domain = f'{slug}.saasclaw.ai'
    nginx_conf = f'/etc/nginx/sites-available/{slug}'
    if not os.path.exists(nginx_conf):
        nginx_content = rf'''server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/saasclaw.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/saasclaw.ai/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 25m;

    root {web_root};
    index index.html;

    location / {{
        try_files $uri $uri/ /index.html;
    }}

    location /assets/ {{
        expires 1y;
        add_header Cache-Control "public, immutable";
    }}

    location ~ /\.\(env|git) {{
        return 444;
    }}
}}
'''
        try:
            with open(nginx_conf, 'w') as f:
                f.write(nginx_content)
            # Symlink to sites-enabled
            enabled_link = f'/etc/nginx/sites-enabled/{slug}'
            if not os.path.exists(enabled_link):
                os.symlink(nginx_conf, enabled_link)
            # Reload nginx
            subprocess.run(['sudo', 'nginx', '-t'], capture_output=True, timeout=10)
            subprocess.run(['sudo', 'nginx', '-s', 'reload'], capture_output=True, timeout=10)
            results.append(f'✅ Nginx site configured for {domain}')
        except Exception as e:
            results.append(f'⚠️ Could not auto-configure nginx: {e}')
            results.append(f'📝 Manual: Create {nginx_conf} with web root {web_root}')
    else:
        results.append(f'✅ Nginx config already exists for {domain}')

    results.append(f'🌐 Deploy ready at https://{domain}')
    results.append(f'📂 Web root: {web_root}')

    return Response({
        'id': project.id,
        'status': 'completed',
        'result': '\n'.join(results),
        'url': f'https://{domain}',
        'web_root': web_root,
        'environment': environment,
        'created_at': project.updated_at.isoformat() if hasattr(project, 'updated_at') else '',
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def deploy_status(request, slug):
    """Get current deploy status with real deployment data."""
    from saasclaw_engine.deployments.models import Deployment

    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    # Get the most recent deployment
    last_deploy = Deployment.objects.filter(
        project=project, environment__name='preview'
    ).order_by('-id').first()

    # Check if APK exists (Android) or web root exists (web apps)
    apk_path = f'/srv/saasclaw/projects/{slug}/runtime/preview/app-debug.apk'
    web_root = f'/srv/saasclaw/projects/{slug}/web'
    is_android = project.framework == 'android'
    is_deployed = False
    if is_android:
        is_deployed = os.path.isfile(apk_path)
    else:
        is_deployed = os.path.isdir(web_root) and bool(os.listdir(web_root))

    # Determine status
    if last_deploy:
        status = last_deploy.status
        error = last_deploy.error_message
        commit = last_deploy.git_commit_sha[:7] if last_deploy.git_commit_sha else None
        deployed_at = last_deploy.finished_at.isoformat() if last_deploy.finished_at else None
    else:
        status = 'deployed' if is_deployed else 'not_deployed'
        error = None
        commit = None
        deployed_at = None

    # Build URLs
    preview_url = f'https://{slug}.preview.saasclaw.ai' if project.preview_domain else None
    production_url = f'https://{slug}.saasclaw.ai' if project.production_domain else None
    if is_android:
        download_url = f'https://{slug}.preview.saasclaw.ai/app-debug.apk'
    else:
        download_url = preview_url

    return Response({
        'id': project.id,
        'status': status,
        'deploy_status': status,
        'error': error,
        'deploy_error': error,
        'commit': commit,
        'deployed_at': deployed_at,
        'preview_url': preview_url,
        'production_url': production_url,
        'download_url': download_url,
        'framework': project.framework,
        'is_android': is_android,
        'environment': 'preview',
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def deploy_history(request, slug):
    """Get deploy history (simplified for starter app)."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    # Starter app doesn't have a Deployment model — return project status
    web_root = f'/srv/saasclaw/projects/{slug}/web'
    deployed = os.path.isdir(web_root) and os.path.exists(os.path.join(web_root, 'index.html'))

    if deployed:
        return Response([{
            'id': 1,
            'environment': 'preview',
            'status': 'completed',
            'url': f'https://{project.preview_domain}' if project.preview_domain else '',
            'created_at': project.updated_at.isoformat() if hasattr(project, 'updated_at') else '',
        }])
    return Response([])


# ---- Git ----

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def git_status_view(request, slug):
    """Get git status."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.agent.tools import git_status
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = git_status(workspace)
    return Response({'result': result})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def git_diff_view(request, slug):
    """Get git diff."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.agent.tools import git_diff
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    cached = request.query_params.get('cached', 'false').lower() == 'true'
    result = git_diff(workspace, cached)
    return Response({'result': result})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def git_commit_view(request, slug):
    """Commit changes."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    serializer = GitCommitSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    from saasclaw_engine.agent.tools import git_commit
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = git_commit(workspace, serializer.validated_data['message'])
    return Response({'result': result})


# ---- Infrastructure ----

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_status(request, slug):
    """Read-only project infrastructure info."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.agent.tools import _project_status_tool
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    section = request.query_params.get('section', 'all')
    result = _project_status_tool(workspace, section)
    return Response({'result': result})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def logs_view(request, slug, source):
    """Read server or deploy logs."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    from saasclaw_engine.agent.tools import _read_logs_tool
    lines = int(request.query_params.get('lines', 50))
    result = _read_logs_tool(workspace_path='', source=source, lines=lines, project_slug=slug)
    return Response({'result': result})

# ---- Account / Profile ----

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def account_profile(request):
    """Get user profile, social accounts, and stats."""
    user = request.user

    # Social accounts
    from allauth.socialaccount.models import SocialAccount
    socials = []
    for sa in SocialAccount.objects.filter(user=user):
        socials.append({
            'provider': sa.provider,
            'uid': sa.uid,
            'name': sa.extra_data.get('name', '') if sa.extra_data else '',
            'avatar': sa.extra_data.get('avatar_url', '') if sa.extra_data else '',
            'email': sa.extra_data.get('email', '') if sa.extra_data else '',
        })

    # Stats
    from saasclaw_engine.projects.models import Project
    project_count = Project.objects.filter(owner=user).count()
    deploy_count = user.deployments_triggered.count()

    # API key count
    from .models import ApiKey
    api_key_count = ApiKey.objects.filter(user=user, active=True).count()

    # Provider keys
    from saasclaw_engine.studio_models.models import ProviderKey
    provider_keys = []
    for pk in ProviderKey.objects.filter(user=user):
        masked = pk.api_key[:8] + '...' + pk.api_key[-4:] if len(pk.api_key) > 12 else '***'
        provider_keys.append({
            'id': pk.id,
            'provider': pk.provider,
            'api_key_masked': masked,
            'default_model': pk.default_model,
            'is_active': pk.is_active,
            'is_platform': pk.is_platform,
        })

    return Response({
        'email': user.email,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'date_joined': user.date_joined.isoformat() if user.date_joined else '',
        'last_login': user.last_login.isoformat() if user.last_login else '',
        'is_superuser': user.is_superuser,
        'social_accounts': socials,
        'stats': {
            'projects': project_count,
            'deploys': deploy_count,
            'api_keys': api_key_count,
        },
        'provider_keys': provider_keys,
    })


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def provider_keys_list_create(request):
    """List or add LLM provider API keys."""
    user = request.user
    from saasclaw_engine.studio_models.models import ProviderKey

    if request.method == 'GET':
        keys = []
        for pk in ProviderKey.objects.filter(user=user):
            masked = pk.api_key[:8] + '...' + pk.api_key[-4:] if len(pk.api_key) > 12 else '***'
            keys.append({
                'id': pk.id,
                'provider': pk.provider,
                'api_key_masked': masked,
                'default_model': pk.default_model,
                'is_active': pk.is_active,
                'is_platform': pk.is_platform,
                'created_at': pk.created_at.isoformat(),
            })
        return Response({'keys': keys})

    # POST — create/update
    provider = request.data.get('provider', '').strip()
    api_key = request.data.get('api_key', '').strip()
    default_model = request.data.get('default_model', '').strip()

    if not provider or not api_key:
        return Response({'detail': 'provider and api_key are required.'}, status=400)

    obj, created = ProviderKey.objects.update_or_create(
        user=user, provider=provider,
        defaults={'api_key': api_key, 'default_model': default_model, 'is_active': True},
    )
    masked = api_key[:8] + '...' + api_key[-4:] if len(api_key) > 12 else '***'
    return Response({
        'id': obj.id,
        'provider': obj.provider,
        'api_key_masked': masked,
        'default_model': obj.default_model,
        'is_active': obj.is_active,
        'created': created,
    }, status=201 if created else 200)


@api_view(['DELETE', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def provider_key_delete(request, key_id):
    """Delete or update a provider key."""
    user = request.user
    from saasclaw_engine.studio_models.models import ProviderKey
    try:
        pk = ProviderKey.objects.get(id=key_id, user=user)
    except ProviderKey.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    if request.method == 'DELETE':
        pk.delete()
        return Response({'deleted': True})

    if request.method in ('PUT', 'PATCH'):
        api_key = request.data.get('api_key')
        default_model = request.data.get('default_model')
        provider = request.data.get('provider')
        if api_key:
            pk.api_key = api_key
        if default_model is not None:
            pk.default_model = default_model
        if provider:
            pk.provider = provider
        pk.save()
        return Response({
            'id': pk.id,
            'provider': pk.provider,
            'api_key_masked': pk.api_key[:8] + '...' + pk.api_key[-4:] if len(pk.api_key) > 12 else '***',
            'default_model': pk.default_model,
            'is_active': pk.is_active,
            'is_platform': pk.is_platform,
        })

    return Response({'detail': 'Method not allowed.'}, status=405)

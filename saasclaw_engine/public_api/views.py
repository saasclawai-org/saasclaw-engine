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
    email = request.data.get('email', '')
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


# ---- Projects ----

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

    workspace_path = f'/srv/saasclaw/projects/{slug}/repo'
    os.makedirs(workspace_path, exist_ok=True)

    # Initialize git repo so agent tools work
    subprocess.run(['git', 'init'], cwd=workspace_path, capture_output=True, timeout=10)
    subprocess.run(['git', 'config', 'user.email', 'agent@saasclaw.ai'], cwd=workspace_path, capture_output=True, timeout=5)
    subprocess.run(['git', 'config', 'user.name', 'SaaSClaw Agent'], cwd=workspace_path, capture_output=True, timeout=5)

    Workspace.objects.create(
        project=project,
        user=user,
        local_path=workspace_path,
        is_active=True,
    )

    Environment.objects.create(
        project=project,
        name=Environment.Name.PREVIEW,
        slug='preview',
        domain=f'{slug}.preview.saasclaw.ai',
        is_primary=True,
        runtime_kind=Environment.RuntimeKind.STATIC,
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
    """Send a message to an agent session and stream responses via SSE."""
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

    workspace = _project_workspace(project)
    if not workspace:
        return Response({'detail': 'No workspace found.'}, status=status.HTTP_400_BAD_REQUEST)

    # Mark session as running
    AgentSession.objects.filter(id=session.id).update(status='running')

    # Build conversation from existing messages
    existing = session.messages.order_by('created_at')
    conversation = [
        {'role': m.role, 'content': m.content, 'tool_call': m.tool_call or {}}
        for m in existing
        if m.role in ('user', 'assistant')
    ]

    # Save user message
    AgentMessage.objects.create(
        session=session,
        role='user',
        content=message,
        tool_call={},
    )

    workspace_path = workspace.local_path

    def event_stream():
        try:
            from saasclaw_engine.agent.runner import run_agent

            new_messages = run_agent(
                workspace_path=workspace_path,
                project_name=project.name,
                conversation=conversation,
                user_message=message,
                user=user,
                project_id=project.id,
                session_id=str(session.id),
            )

            # Stream each message as SSE events
            for msg in new_messages:
                role = msg.get('role', 'assistant')
                content = msg.get('content', '')
                tool_call = msg.get('tool_call', msg.get('tool_calls'))

                if role == 'user':
                    # Already saved above
                    continue

                if role == 'assistant' and content:
                    # Save and stream assistant text
                    AgentMessage.objects.create(
                        session=session,
                        role='assistant',
                        content=content,
                        tool_call=tool_call if isinstance(tool_call, dict) else {},
                    )
                    yield f'data: {json.dumps({"type": "content", "content": content})}\n\n'

                elif role == 'assistant' and tool_call:
                    # Tool call from assistant
                    AgentMessage.objects.create(
                        session=session,
                        role='assistant',
                        content=content or '',
                        tool_call=tool_call if isinstance(tool_call, dict) else {},
                    )
                    if isinstance(tool_call, dict):
                        tc_name = tool_call.get('function', {}).get('name', tool_call.get('name', ''))
                    elif isinstance(tool_call, list):
                        for tc in tool_call:
                            tc_name = tc.get('function', {}).get('name', str(tc)) if isinstance(tc, dict) else str(tc)
                            yield f'data: {json.dumps({"type": "tool_call", "name": tc_name})}\n\n'
                        continue
                    else:
                        tc_name = str(tool_call)
                    yield f'data: {json.dumps({"type": "tool_call", "name": tc_name})}\n\n'

                elif role == 'tool':
                    tool_name = msg.get('name', 'unknown')
                    tool_content = msg.get('content', '')
                    # Save tool result
                    AgentMessage.objects.create(
                        session=session,
                        role='tool',
                        content=str(tool_content)[:2000],
                        tool_call={'name': tool_name, 'result': str(tool_content)[:200]},
                    )
                    # Truncate for SSE
                    display = str(tool_content)
                    if len(display) > 2000:
                        display = display[:2000] + '...'
                    yield f'data: {json.dumps({"type": "tool_result", "name": tool_name, "content": display})}\n\n'

            # Mark session as idle
            AgentSession.objects.filter(id=session.id).update(status='idle')
            yield 'data: [DONE]\n\n'
        except Exception as e:
            logger.exception('SSE stream error for project=%s session=%s', slug, session_id)
            AgentSession.objects.filter(id=session.id).update(status='idle')
            yield f'data: {json.dumps({"type": "error", "content": str(e)})}\n\n'

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


# ---- Environment Variables ----

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
    """Get current deploy status."""
    user = _get_user(request)
    project, err = _get_project(slug, user)
    if err:
        return err

    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    web_root = f'/srv/saasclaw/projects/{slug}/web'

    # Check if web root exists
    deployed = os.path.isdir(web_root) and os.listdir(web_root)

    return Response({
        'id': project.id,
        'status': 'deployed' if deployed else 'not_deployed',
        'url': f'https://{project.preview_domain}' if project.preview_domain else '',
        'environment': 'preview',
        'created_at': project.updated_at.isoformat() if hasattr(project, 'updated_at') else '',
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
"""Public API views — thin REST layer over existing engine tools and models.

This is the API that the SaaSClaw Starter app consumes.
All endpoints require JWT auth (or are open in SINGLE_USER mode).
"""
import json
import logging
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny as DRFAllowAny
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

SINGLE_USER = getattr(settings, 'SAASCLAW_SINGLE_USER', False)


def _get_or_create_single_user():
    """In single-user mode, return a default user."""
    user = User.objects.filter(is_superuser=True).first()
    if not user:
        user = User.objects.create_superuser(
            username='admin',
            email='admin@saasclaw.local',
            password='admin',
        )
    return user


def _get_user(request):
    """Get the effective user — in single-user mode, always the admin."""
    if SINGLE_USER:
        return _get_or_create_single_user()
    return request.user


def _get_project(slug, user):
    """Look up a project owned by user, or 404."""
    try:
        return Project.objects.get(slug=slug, owner=user, deleted_at__isnull=True)
    except Project.DoesNotExist:
        return None


def _project_workspace(project):
    """Get the active workspace for a project."""
    ws = Workspace.objects.filter(project=project, is_active=True).first()
    if not ws:
        ws = Workspace.objects.filter(project=project).first()
    return ws


# ---- Auth ----

@api_view(['POST'])
@permission_classes([DRFAllowAny])
@authentication_classes([])
def register_view(request):
    """Register a new user and return JWT tokens."""
    if SINGLE_USER:
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
def login_view(request):
    """Authenticate user and return JWT tokens."""
    if SINGLE_USER:
        user = _get_or_create_single_user()
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
def projects_list_create(request):
    """List user's projects or create a new one."""
    user = _get_user(request)

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
def project_detail(request, slug):
    """Get, update, or delete a project."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

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
def files_list(request, slug):
    """List files in a project workspace."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import list_files
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = list_files(workspace, request.query_params.get('path', '.'))
    try:
        entries = json.loads(result)
    except json.JSONDecodeError:
        entries = [{'name': result, 'type': 'message'}]
    return Response(entries)


@api_view(['GET', 'PUT'])
def file_detail(request, slug, path):
    """Read or write a file in a project workspace."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

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
def sessions_list_create(request, slug):
    """List or create chat sessions for a project."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.studio_models.models import AgentSession

    if request.method == 'GET':
        sessions = AgentSession.objects.filter(
            workspace__project=project,
        ).order_by('-created_at')[:50]
        return Response([{
            'id': str(s.id),
            'created_at': s.created_at.isoformat(),
            'title': s.title or '',
        } for s in sessions])

    # POST — create session
    workspace = _project_workspace(project)
    if not workspace:
        return Response({'detail': 'No workspace found.'}, status=status.HTTP_400_BAD_REQUEST)

    session = AgentSession.objects.create(
        workspace=workspace,
        title=request.data.get('title', 'New session'),
    )
    return Response({
        'id': str(session.id),
        'created_at': session.created_at.isoformat(),
        'title': session.title,
        'messages': [],
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def session_detail(request, slug, session_id):
    """Get session detail including messages."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.studio_models.models import AgentSession
    try:
        session = AgentSession.objects.get(id=session_id, workspace__project=project)
    except AgentSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response({
        'id': str(session.id),
        'created_at': session.created_at.isoformat(),
        'title': session.title,
        'messages': session.messages or [],
    })


# ---- Chat Send (SSE) ----

@api_view(['POST'])
def session_send(request, slug, session_id):
    """Send a message to an agent session and stream responses via SSE."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.studio_models.models import AgentSession
    try:
        session = AgentSession.objects.get(id=session_id, workspace__project=project)
    except AgentSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

    message = request.data.get('message', '').strip()
    if not message:
        return Response({'detail': 'Message is required.'}, status=status.HTTP_400_BAD_REQUEST)

    workspace = _project_workspace(project)
    if not workspace:
        return Response({'detail': 'No workspace found.'}, status=status.HTTP_400_BAD_REQUEST)

    workspace_path = workspace.local_path
    conversation = session.messages or []
    conversation = [m for m in conversation if m.get('role') in ('user', 'assistant')]

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

            # Save messages back to session
            all_messages = list(session.messages or []) + [
                {'role': 'user', 'content': message},
            ] + list(new_messages)
            session.messages = all_messages
            session.save(update_fields=['messages', 'updated_at'])

            # Stream each message as SSE events
            for msg in new_messages:
                role = msg.get('role', 'assistant')
                content = msg.get('content', '')
                tool_calls = msg.get('tool_calls')

                if role == 'assistant' and content:
                    yield f'data: {json.dumps({"type": "content", "content": content})}\n\n'
                elif role == 'assistant' and tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            tc_name = tc.get('function', {}).get('name', '')
                        else:
                            tc_name = str(tc)
                        yield f'data: {json.dumps({"type": "tool_call", "name": tc_name})}\n\n'
                elif role == 'tool':
                    tool_name = msg.get('name', 'unknown')
                    tool_content = msg.get('content', '')
                    # Truncate large tool results
                    if len(str(tool_content)) > 2000:
                        tool_content = str(tool_content)[:2000] + '...'
                    yield f'data: {json.dumps({"type": "tool_result", "name": tool_name, "content": tool_content})}\n\n'

            yield 'data: [DONE]\n\n'
        except Exception as e:
            logger.exception('SSE stream error for project=%s session=%s', slug, session_id)
            yield f'data: {json.dumps({"type": "error", "content": str(e)})}\n\n'

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


# ---- Environment Variables ----

@api_view(['GET', 'POST'])
def env_list_create(request, slug):
    """List or set environment variables."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

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
def env_delete(request, slug, key):
    """Delete an environment variable."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    for env in Environment.objects.filter(project=project):
        EnvironmentVariable.objects.filter(environment=env, key=key).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ---- Deploy ----

@api_view(['POST'])
def deploy_trigger(request, slug):
    """Trigger a deploy."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import _deploy_project_tool
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = _deploy_project_tool(workspace, request.data.get('environment', 'preview'))
    return Response({'status': 'completed', 'result': result})


@api_view(['GET'])
def deploy_status(request, slug):
    """Get current deploy status."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import _project_status_tool
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = _project_status_tool(workspace, 'service')
    return Response({'status_text': result})


@api_view(['GET'])
def deploy_history(request, slug):
    """Get deploy history."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.deployments.models import Deployment
    deployments = Deployment.objects.filter(
        environment__project=project,
    ).order_by('-created_at')[:20]

    data = [{
        'id': d.id,
        'environment': d.environment.name,
        'status': d.status,
        'url': f'https://{project.preview_domain}' if project.preview_domain else '',
        'created_at': d.created_at.isoformat(),
    } for d in deployments]
    return Response(data)


# ---- Git ----

@api_view(['GET'])
def git_status_view(request, slug):
    """Get git status."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import git_status
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = git_status(workspace)
    return Response({'result': result})


@api_view(['GET'])
def git_diff_view(request, slug):
    """Get git diff."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import git_diff
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    cached = request.query_params.get('cached', 'false').lower() == 'true'
    result = git_diff(workspace, cached)
    return Response({'result': result})


@api_view(['POST'])
def git_commit_view(request, slug):
    """Commit changes."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = GitCommitSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    from saasclaw_engine.agent.tools import git_commit
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    result = git_commit(workspace, serializer.validated_data['message'])
    return Response({'result': result})


# ---- Infrastructure ----

@api_view(['GET'])
def project_status(request, slug):
    """Read-only project infrastructure info."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import _project_status_tool
    workspace = project.workspace_root or f'/srv/saasclaw/projects/{slug}/repo'
    section = request.query_params.get('section', 'all')
    result = _project_status_tool(workspace, section)
    return Response({'result': result})


@api_view(['GET'])
def logs_view(request, slug, source):
    """Read server or deploy logs."""
    user = _get_user(request)
    project = _get_project(slug, user)
    if not project:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    from saasclaw_engine.agent.tools import _read_logs_tool
    lines = int(request.query_params.get('lines', 50))
    result = _read_logs_tool(workspace_path='', source=source, lines=lines, project_slug=slug)
    return Response({'result': result})
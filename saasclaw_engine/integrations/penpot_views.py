"""Penpot API views — status, provision, projects, files, import.

All endpoints require JWT auth (IsAuthenticated).
Uses cookie-based auth to communicate with the internal Penpot instance.
"""
import json
import logging
import secrets
import string
import subprocess
import requests

from django.contrib.auth import get_user_model
from django.http import JsonResponse, HttpResponseRedirect, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from .models import PenpotConnection
from .penpot import PenpotClient, extract_file_summary, format_penpot_tokens_for_prompt, format_tokens_for_compose

logger = logging.getLogger(__name__)
User = get_user_model()


def _get_client(conn: PenpotConnection) -> PenpotClient:
    """Create a PenpotClient from a PenpotConnection record."""
    client = PenpotClient(email=conn.penpot_email, password=conn.penpot_password)
    client.login()
    return client


def _generate_password(length: int = 24) -> str:
    """Generate a random password for Penpot accounts."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _provision_penpot_account(user) -> PenpotConnection:
    """Create a Penpot account for a SaaSClaw user.

    1. Generate a random password
    2. Create the Penpot profile via docker exec
    3. Store the PenpotConnection record
    4. Create a default team and project
    """
    email = user.email or f"{user.username}@saasclaw.ai"
    fullname = user.get_full_name() or user.username
    password = _generate_password()

    # Create profile in Penpot via docker exec
    cmd = [
        "sudo", "docker", "exec", "penpot-penpot-backend-1",
        "python3", "manage.py", "create-profile",
        "-e", email,
        "-p", password,
        "-n", fullname,
        "--skip-tutorial",
        "--skip-walkthrough",
        "-f",  # force if already exists
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("Penpot create-profile returned %d: %s", result.returncode, result.stderr)
    except Exception as e:
        logger.error("Penpot create-profile failed: %s", e)

    # Log in to get the profile info (user ID, default team, project)
    client = PenpotClient(email=email, password=password)
    try:
        profile = client.login()
    except Exception as e:
        logger.error("Penpot login after provision failed: %s", e)
        # Store the connection anyway so we can retry later
        conn = PenpotConnection.objects.update_or_create(
            user=user,
            defaults={
                'penpot_email': email,
                'penpot_password': password,
                'penpot_user_id': '',
                'penpot_team_id': '',
                'penpot_project_id': '',
            },
        )[0]
        return conn

    penpot_user_id = profile.get('id', '')
    default_team_id = profile.get('defaultTeamId', '')
    default_project_id = profile.get('defaultProjectId', '')

    conn = PenpotConnection.objects.update_or_create(
        user=user,
        defaults={
            'penpot_email': email,
            'penpot_password': password,
            'penpot_user_id': penpot_user_id,
            'penpot_team_id': default_team_id,
            'penpot_project_id': default_project_id,
        },
    )[0]

    logger.info("Penpot account provisioned for %s (penpot user: %s)", email, penpot_user_id)
    return conn


# --- API Views ---

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def penpot_status(request):
    """Check if user has a Penpot connection."""
    conn = PenpotConnection.objects.filter(user=request.user).first()
    if not conn:
        return JsonResponse({
            'connected': False,
            'email': '',
        })
    return JsonResponse({
        'connected': conn.is_connected,
        'email': conn.penpot_email,
        'penpot_user_id': conn.penpot_user_id,
        'has_default_project': bool(conn.penpot_project_id),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def penpot_provision(request):
    """Create/provision a Penpot account for the current user."""
    conn = PenpotConnection.objects.filter(user=request.user).first()
    if conn and conn.is_connected:
        return JsonResponse({
            'ok': True,
            'already_exists': True,
            'email': conn.penpot_email,
        })

    try:
        conn = _provision_penpot_account(request.user)
        return JsonResponse({
            'ok': True,
            'email': conn.penpot_email,
            'penpot_user_id': conn.penpot_user_id,
        })
    except Exception as e:
        logger.error("Penpot provision failed for %s: %s", request.user.email, e)
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def penpot_projects(request):
    """List all Penpot projects for the user."""
    conn = PenpotConnection.objects.filter(user=request.user).first()
    if not conn or not conn.is_connected:
        return JsonResponse({'error': 'Penpot not connected. Call /penpot/provision/ first.'}, status=403)

    try:
        client = _get_client(conn)
        projects = client.get_all_projects()
        return JsonResponse({'projects': projects})
    except Exception as e:
        logger.error("Penpot get_all_projects failed: %s", e)
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def penpot_files(request, project_id):
    """List files in a Penpot project."""
    conn = PenpotConnection.objects.filter(user=request.user).first()
    if not conn or not conn.is_connected:
        return JsonResponse({'error': 'Penpot not connected'}, status=403)

    try:
        client = _get_client(conn)
        files = client.get_project_files(project_id)
        return JsonResponse({'files': files})
    except Exception as e:
        logger.error("Penpot get_project_files failed: %s", e)
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def penpot_sso(request):
    """SSO redirect: logs user into Penpot and redirects with auth cookie.

    Uses DRF IsAuthenticated (session auth works since SessionAuthentication is enabled).
    """
    conn = PenpotConnection.objects.filter(user=request.user).first()
    if not conn or not conn.is_connected:
        # Try to provision on the fly
        try:
            conn = _provision_penpot_account(request.user)
        except Exception as e:
            return JsonResponse({'error': f'Penpot provisioning failed: {e}'}, status=500)

    # Login to Penpot to get a fresh auth cookie
    try:
        resp = requests.post(
            f'{_get_penpot_base_url()}/api/rpc/command/login-with-password',
            json={'email': conn.penpot_email, 'password': conn.penpot_password},
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("Penpot SSO login failed: %s", e)
        return JsonResponse({'error': 'Penpot login failed'}, status=502)

    # Extract auth cookie from response
    auth_cookie = None
    for cookie in resp.cookies:
        if cookie.name == 'auth-token':
            auth_cookie = cookie
            break

    if not auth_cookie:
        # Try from Set-Cookie header directly
        set_cookie = resp.headers.get('Set-Cookie', '')
        if 'auth-token=' in set_cookie:
            token_value = set_cookie.split('auth-token=')[1].split(';')[0]
            # Build redirect with cookie set on shared domain
            response = HttpResponseRedirect('https://design.saasclaw.ai/')
            response.set_cookie(
                'auth-token',
                token_value,
                domain='.saasclaw.ai',
                httponly=True,
                secure=True,
                samesite='Lax',
            )
            return response
        logger.error("Penpot SSO: no auth-token cookie in response")
        return JsonResponse({'error': 'No auth cookie from Penpot'}, status=502)

    # Set cookie on shared domain and redirect
    response = HttpResponseRedirect('https://design.saasclaw.ai/')
    response.set_cookie(
        'auth-token',
        auth_cookie.value,
        domain='.saasclaw.ai',
        httponly=True,
        secure=True,
        samesite='Lax',
    )
    logger.info("Penpot SSO redirect for user %s", request.user.username)
    return response


def _get_penpot_base_url() -> str:
    """Get Penpot base URL from settings."""
    from .penpot import PENPOT_BASE_URL
    return getattr(settings, 'PENPOT_BASE_URL', PENPOT_BASE_URL)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def penpot_import(request, file_id):
    """Export file data as design tokens + component list for wizard context."""
    conn = PenpotConnection.objects.filter(user=request.user).first()
    if not conn or not conn.is_connected:
        return JsonResponse({'error': 'Penpot not connected'}, status=403)

    try:
        client = _get_client(conn)
        file_data = client.get_file(file_id)
        summary = extract_file_summary(file_data)
        return JsonResponse(summary)
    except Exception as e:
        logger.error("Penpot import failed: %s", e)
        return JsonResponse({'error': str(e)}, status=500)
"""Penpot API views — status, provision, projects, files, import.

All endpoints require JWT auth (IsAuthenticated).
Uses cookie-based auth to communicate with the internal Penpot instance.
"""
import json
import logging
import secrets
import string
import subprocess

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from .models import PenpotConnection
from .penpot import PenpotClient, extract_file_summary, format_penpot_tokens_for_prompt

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
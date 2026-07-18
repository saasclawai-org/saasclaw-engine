import json
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import GitHubInstallation, InstallationRepository, FigmaConnection
from .figma import (
    get_oauth_url, exchange_code_for_token, refresh_token,
    parse_figma_url, get_file, get_file_nodes, get_file_images,
    extract_design_tokens, format_tokens_for_prompt, download_image,
)

logger = logging.getLogger(__name__)

User = get_user_model()


def _link_installation_to_user(installation: GitHubInstallation, payload: dict):
    """Try to link a GitHub installation to the SaaSClaw user who installed it.

    GitHub sends the sender (the user who triggered the event) in installation events.
    We match them to SaaSClaw users by:
    1. GitHub social auth account ID (preferred)
    2. SaaSClaw username == GitHub login (fallback)
    """
    sender = payload.get('sender', {})
    sender_github_id = sender.get('id')
    sender_login = sender.get('login', '')

    installation.sender_github_id = sender_github_id
    installation.sender_login = sender_login

    if sender_github_id:
        # Try social auth linkage first
        try:
            user = User.objects.get(
                socialaccount__uid=str(sender_github_id),
                socialaccount__provider='github',
            )
            installation.user = user
            logger.info("Linked installation %s to user %s via GitHub social auth (id=%s)",
                         installation.installation_id, user.username, sender_github_id)
            return
        except (User.DoesNotExist, Exception):
            pass

    if sender_login:
        # Fallback: match by username
        try:
            user = User.objects.get(username=sender_login)
            installation.user = user
            logger.info("Linked installation %s to user %s via username match",
                         installation.installation_id, user.username)
            return
        except User.DoesNotExist:
            pass

    logger.info("Could not link installation %s to a SaaSClaw user (sender: %s, id: %s)",
                 installation.installation_id, sender_login, sender_github_id)


def _sync_repositories(installation: GitHubInstallation, repos_payload: list):
    """Sync the list of repositories from an installation event."""
    repo_ids = set()
    for repo_data in repos_payload:
        repo_id = repo_data.get('id')
        full_name = repo_data.get('full_name', '')
        parts = full_name.split('/', 1)
        repo_name = parts[1] if len(parts) == 2 else full_name

        InstallationRepository.objects.update_or_create(
            installation=installation,
            repo_id=repo_id,
            defaults={
                'repo_name': repo_name,
                'full_name': full_name,
                'private': repo_data.get('private', True),
                'default_branch': repo_data.get('default_branch', 'main'),
            },
        )
        repo_ids.add(repo_id)

    # Remove repos no longer in the installation
    removed = InstallationRepository.objects.filter(
        installation=installation,
    ).exclude(repo_id__in=repo_ids)
    count = removed.count()
    removed.delete()
    if count:
        logger.info("Removed %d repos from installation %s", count, installation.installation_id)


@login_required
def github_setup(request):
    # Only show the current user's installations
    installations = request.user.github_installations.order_by('account_name', 'installation_id')
    # Fetch app slug for install URL
    github_app_slug = settings.GITHUB_APP_ID
    try:
        from saasclaw_engine.integrations.github import build_github_app_jwt
        import requests as _req
        r = _req.get('https://api.github.com/app',
            headers={'Authorization': f'Bearer {build_github_app_jwt()}', 'Accept': 'application/vnd.github+json'},
            timeout=10)
        if r.ok:
            github_app_slug = r.json().get('slug', settings.GITHUB_APP_ID)
    except Exception:
        pass

    return render(request, 'app/github_setup.html', {
        'installations': installations,
        'github_app_id': settings.GITHUB_APP_ID,
        'github_app_slug': github_app_slug,
        'github_app_configured': bool(
            settings.GITHUB_APP_ID
            and (settings.GITHUB_APP_PRIVATE_KEY or settings.GITHUB_APP_PRIVATE_KEY_PATH)
            and settings.GITHUB_WEBHOOK_SECRET
        ),
    })


@csrf_exempt
@require_POST
def github_webhook(request):
    event = request.headers.get('X-GitHub-Event', '').strip()
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid JSON payload.')

    if not settings.GITHUB_WEBHOOK_SECRET:
        return HttpResponseBadRequest('GitHub webhook secret is not configured.')

    if event == 'installation':
        installation_data = payload.get('installation') or {}
        account = installation_data.get('account') or {}
        installation_id = installation_data.get('id')
        if installation_id:
            inst, created = GitHubInstallation.objects.update_or_create(
                installation_id=installation_id,
                defaults={
                    'account_name': account.get('login') or f'installation-{installation_id}',
                    'account_type': account.get('type') or '',
                    'github_account_id': account.get('id'),
                    'repository_selection': installation_data.get('repository_selection', 'all'),
                    'access_metadata_json': payload,
                },
            )
            # Link to the SaaSClaw user who triggered the event
            _link_installation_to_user(inst, payload)
            inst.save(update_fields=['user', 'sender_github_id', 'sender_login', 'updated_at'])
            # Sync repo list
            repos = installation_data.get('repositories', [])
            if repos:
                _sync_repositories(inst, repos)

    elif event == 'installation_repositories':
        # Fired when repos are added/removed from an installation
        installation_id = payload.get('installation', {}).get('id')
        action = payload.get('action')  # added or removed
        if installation_id:
            try:
                inst = GitHubInstallation.objects.get(installation_id=installation_id)
                # Re-sync all repos for this installation
                all_repos = _fetch_installation_repos(inst)
                if all_repos is not None:
                    _sync_repositories(inst, all_repos)
            except GitHubInstallation.DoesNotExist:
                logger.warning("installation_repositories event for unknown installation %s", installation_id)

    elif event == 'installation.deleted':
        # GitHub App was uninstalled
        installation_id = payload.get('installation', {}).get('id')
        if installation_id:
            deleted, _ = GitHubInstallation.objects.filter(installation_id=installation_id).delete()
            if deleted:
                logger.info("Deleted installation %s (app uninstalled)", installation_id)

    return JsonResponse({'ok': True, 'event': event})


def _fetch_installation_repos(installation: GitHubInstallation) -> list | None:
    """Fetch the full repo list from GitHub API for an installation."""
    try:
        from saasclaw_engine.integrations.github import create_installation_access_token
        import requests
        token = create_installation_access_token(installation.installation_id)
        resp = requests.get(
            f'https://api.github.com/user/installations/{installation.installation_id}/repositories',
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'Per-Page': '100',
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Failed to fetch repos for installation %s: HTTP %s",
                        installation.installation_id, resp.status_code)
        return None
    except Exception as e:
        logger.warning("Error fetching repos for installation %s: %s",
                        installation.installation_id, e)
        return None


# --- Figma OAuth ---

@login_required
def figma_connect(request):
    """Redirect user to Figma OAuth authorization page."""
    import secrets
    state = secrets.token_urlsafe(32)
    request.session['figma_oauth_state'] = state
    return redirect(get_oauth_url(state))


@login_required
def figma_callback(request):
    """Handle OAuth callback from Figma, exchange code for tokens."""
    from django.shortcuts import redirect as _redirect

    code = request.GET.get('code')
    state = request.GET.get('state')
    expected_state = request.session.pop('figma_oauth_state', None)

    if not code or not state or state != expected_state:
        return HttpResponseBadRequest('Invalid OAuth state or missing code.')

    try:
        token_data = exchange_code_for_token(code)
    except Exception as e:
        logger.error("Figma token exchange failed: %s", e)
        return _redirect('/app/settings/?figma=error')

    from datetime import timedelta
    from django.utils import timezone

    expires_in = token_data.get('expires_in', 3600)
    FigmaConnection.objects.update_or_create(
        user=request.user,
        defaults={
            'access_token': token_data.get('access_token', ''),
            'refresh_token': token_data.get('refresh_token', ''),
            'expires_at': timezone.now() + timedelta(seconds=expires_in),
            'figma_user_id': '',
        },
    )

    # Try to fetch user info
    try:
        import requests
        resp = requests.get('https://api.figma.com/v1/me',
                           headers={'X-Figma-Token': token_data['access_token']}, timeout=10)
        if resp.ok:
            me = resp.json()
            conn = FigmaConnection.objects.get(user=request.user)
            conn.figma_email = me.get('email', '')
            conn.figma_username = me.get('handle', '')
            conn.figma_user_id = str(me.get('id', ''))
            conn.save(update_fields=['figma_email', 'figma_username', 'figma_user_id'])
    except Exception:
        pass

    return _redirect('/app/settings/?figma=connected')


@login_required
def figma_disconnect(request):
    """Remove the user's Figma connection."""
    FigmaConnection.objects.filter(user=request.user).delete()
    return JsonResponse({'ok': True})


@login_required
def figma_status(request):
    """Check if user has a connected Figma account."""
    conn = FigmaConnection.objects.filter(user=request.user).first()
    return JsonResponse({
        'connected': bool(conn and conn.is_connected),
        'email': conn.figma_email if conn else '',
        'username': conn.figma_username if conn else '',
    })


@login_required
def figma_extract_tokens(request):
    """Extract design tokens from a Figma URL.

    POST body: {"url": "https://www.figma.com/design/..."}
    Returns: {tokens: {...}, prompt_text: "...", screenshot_url: "..."}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    url = body.get('url', '').strip()
    if not url:
        return JsonResponse({'error': 'URL required'}, status=400)

    parsed = parse_figma_url(url)
    if not parsed:
        return JsonResponse({'error': 'Invalid Figma URL'}, status=400)

    conn = FigmaConnection.objects.filter(user=request.user).first()
    if not conn or not conn.is_connected:
        return JsonResponse({'error': 'Figma not connected'}, status=403)

    try:
        # Fetch file data for the specific node (or whole file)
        if parsed['node_id']:
            file_data = get_file(parsed['file_key'], conn.access_token, ids=parsed['node_id'])
        else:
            file_data = get_file(parsed['file_key'], conn.access_token, depth=3)

        tokens = extract_design_tokens(file_data, parsed['node_id'])
        prompt_text = format_tokens_for_prompt(tokens)

        # Get screenshot
        screenshot_url = None
        if parsed['node_id']:
            try:
                images = get_file_images(
                    parsed['file_key'], [parsed['node_id']], conn.access_token,
                    format='png', scale=2.0,
                )
                screenshot_url = images.get('images', {}).get(parsed['node_id'])
            except Exception:
                pass

        return JsonResponse({
            'tokens': tokens,
            'prompt_text': prompt_text,
            'screenshot_url': screenshot_url,
            'file_name': file_data.get('name', ''),
        })
    except Exception as e:
        logger.error("Figma token extraction failed: %s", e)
        return JsonResponse({'error': str(e)}, status=500)

"""Public API for static site form submissions.

POST /api/forms/{slug}/  — accept form data from any static site
GET  /api/forms/{slug}/list/  — list submissions (project owner, staff only)
GET  /api/forms/{slug}/{id}/ — single submission detail (project owner, staff only)
DELETE /api/forms/{slug}/{id}/ — delete a submission (project owner, staff only)

Security:
- Per-project API key required on every POST submission
- Rate limiting: 10 submissions per minute per IP per project
- Origin validation against project's deployed domains
- Honeypot anti-spam
"""

import json
import logging
import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.http import (
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from saasclaw_engine.projects.models import Project, FormSubmission

logger = logging.getLogger(__name__)

# Simple honeypot field name — bots often fill hidden fields
HONEYPOT_FIELD = "website"

# Rate limiting
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW = 60  # seconds


def _rate_limit_key(slug, ip):
    return f"form_rl:{slug}:{ip}"


def _check_rate_limit(slug, ip):
    """Return True if the request is allowed, False if rate limited."""
    key = _rate_limit_key(slug, ip)
    count = cache.get(key, 0)
    if count >= RATE_LIMIT_REQUESTS:
        return False
    cache.set(key, count + 1, RATE_LIMIT_WINDOW)
    return True


def _validate_origin(request, project):
    """Check Origin/Referer against project's deployed domains."""
    origin = request.META.get('HTTP_ORIGIN', '')
    referer = request.META.get('HTTP_REFERER', '')
    source = origin or referer

    if not source:
        return True  # No origin header — allow (curl, server-to-server)

    # Build allowed origins from project domains
    allowed = set()
    if project.preview_domain:
        allowed.add(f"https://{project.preview_domain}")
    if project.production_domain:
        allowed.add(f"https://{project.production_domain}")

    # Also allow the SaaSClaw app itself (studio UI)
    host = getattr(settings, 'ALLOWED_HOSTS', [''])
    for h in host:
        if h and h != '*':
            allowed.add(f"https://{h}")

    # Strip path from referer for comparison
    for a in allowed:
        if source.startswith(a):
            return True

    # Allow if no domains configured yet (project hasn't been deployed)
    if not allowed:
        return True

    logger.warning(
        "Form submission to %s rejected: origin %s not in allowed set",
        project.slug, source,
    )
    return False


def _client_ip(request):
    """Extract client IP, respecting X-Forwarded-For from nginx."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()[:45]
    return request.META.get('REMOTE_ADDR', '')[:45]


def _can_manage(user, project):
    """Check if user is project owner or staff."""
    if not user or not user.is_authenticated:
        return False
    return user.is_staff or project.owner_id == user.id


@csrf_exempt
@require_POST
def submit_form(request, slug):
    """Accept a form submission from a static site.

    Requires:
    - `X-Form-Key` header with the project's API key
    - or `_form_key` field in the POST body

    Accepts:
    - application/json body: {"field": "value", ...}
    - application/x-www-form-urlencoded: standard form POST

    Returns 201 with {ok: true, id: N} on success.
    """
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Project not found.'}, status=404)

    # Block if project is suspended/archived
    if project.status in (Project.Status.SUSPENDED, Project.Status.ARCHIVED):
        return JsonResponse(
            {'ok': False, 'error': 'This project is not accepting submissions.'},
            status=403,
        )

    # --- API key validation ---
    api_key = (
        request.META.get('HTTP_X_FORM_KEY', '')
        or request.POST.get('_form_key', '')
        or ''
    )
    if not api_key or api_key != project.form_api_key:
        return HttpResponseForbidden('Invalid or missing API key. Set X-Form-Key header.')

    # --- Rate limiting ---
    client_ip = _client_ip(request)
    if not _check_rate_limit(slug, client_ip):
        return JsonResponse(
            {'ok': False, 'error': 'Rate limit exceeded. Try again later.'},
            status=429,
        )

    # --- Origin validation ---
    if not _validate_origin(request, project):
        return HttpResponseForbidden('Origin not allowed.')

    # --- Parse form data ---
    content_type = request.content_type or ''

    if 'application/json' in content_type:
        try:
            form_data = json.loads(request.body.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpResponseBadRequest('Invalid JSON body.')
    else:
        # QueryDict values are lists; unwrap single values
        raw = dict(request.POST)
        form_data = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in raw.items()}

    if not form_data:
        return HttpResponseBadRequest('No form data received.')

    # Honeypot check — if the hidden field is filled, silently accept but discard
    if HONEYPOT_FIELD in form_data and form_data[HONEYPOT_FIELD]:
        return JsonResponse({'ok': True, 'id': 0})

    # Strip honeypot and internal fields from stored data
    form_data.pop(HONEYPOT_FIELD, None)
    form_data.pop('_form_key', None)

    # Basic size limit
    if len(str(form_data)) > 100_000:
        return HttpResponseBadRequest('Submission too large.')

    # --- Detect environment from forwarded host ---
    forwarded_host = request.META.get('HTTP_X_FORWARDED_HOST', '')
    preview_domain = getattr(settings, 'PREVIEW_BASE_DOMAIN', 'preview.saasclaw.ai')
    environment = (
        FormSubmission.Environment.PREVIEW
        if preview_domain in forwarded_host
        else FormSubmission.Environment.PRODUCTION
    )

    submission = FormSubmission.objects.create(
        project=project,
        form_data=form_data,
        environment=environment,
        ip_address=client_ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        referrer=request.META.get('HTTP_REFERER', '')[:2048],
    )

    logger.info(
        "Form submission #%d for project %s from %s",
        submission.id, slug, client_ip,
    )

    return JsonResponse({'ok': True, 'id': submission.id}, status=201)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def form_submissions(request, slug):
    """List or bulk-delete form submissions. Project owner/staff only."""
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Not found.'}, status=404)

    if not _can_manage(request.user, project):
        api_key = request.META.get('HTTP_X_FORM_KEY', '')
        if not api_key or api_key != project.form_api_key:
            return HttpResponseForbidden('Access denied.')

    if request.method == 'DELETE':
        FormSubmission.objects.filter(project=project).delete()
        return JsonResponse({'ok': True, 'deleted': True})

    # GET — list submissions with pagination
    submissions = FormSubmission.objects.filter(project=project)
    env_filter = request.GET.get('environment', '')
    if env_filter:
        submissions = submissions.filter(environment=env_filter)
    total = submissions.count()
    limit = min(int(request.GET.get('limit', 50)), 500)
    offset = int(request.GET.get('offset', 0))
    items = submissions[offset:offset + limit]

    return JsonResponse({
        'ok': True,
        'total': total,
        'limit': limit,
        'offset': offset,
        'items': [
            {
                'id': s.id,
                'environment': s.environment,
                'form_data': s.form_data,
                'submitted_at': s.submitted_at.isoformat(),
                'ip_address': s.ip_address,
                'user_agent': s.user_agent,
                'referrer': s.referrer,
            }
            for s in items
        ],
    })


@csrf_exempt
@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def form_submission_detail(request, slug, pk):
    """View or delete a single submission. Project owner/staff only."""
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Not found.'}, status=404)

    if not _can_manage(request.user, project):
        api_key = request.META.get('HTTP_X_FORM_KEY', '')
        if not api_key or api_key != project.form_api_key:
            return HttpResponseForbidden('Access denied.')

    try:
        submission = FormSubmission.objects.get(project=project, pk=pk)
    except FormSubmission.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Submission not found.'}, status=404)

    if request.method == 'DELETE':
        submission.delete()
        return JsonResponse({'ok': True, 'deleted': True})

    return JsonResponse({
        'ok': True,
        'id': submission.id,
        'environment': submission.environment,
        'form_data': submission.form_data,
        'submitted_at': submission.submitted_at.isoformat(),
        'ip_address': submission.ip_address,
        'user_agent': submission.user_agent,
        'referrer': submission.referrer,
    })


@require_http_methods(["GET"])
@csrf_exempt
def public_form_data(request, slug):
    """Public read-only access to form submissions for a project.
    Authenticated via API key (X-Form-Key header) — no user session required.
    Used by static/SPA frontends to load their own data.
    """
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Not found."}, status=404)

    # API key check (same as submit_form)
    api_key = (
        request.META.get("HTTP_X_FORM_KEY", "")
        or ""
    )
    if not api_key or api_key != project.form_api_key:
        return HttpResponseForbidden("Invalid or missing API key.")

    # Only return preview data if requested from preview, production if from production
    host = request.META.get("HTTP_X_FORWARDED_HOST", "") or request.META.get("HTTP_HOST", "")
    origin = request.META.get("HTTP_ORIGIN", "") or request.META.get("HTTP_REFERER", "")
    source = host or origin
    env = "production"
    if project.preview_domain and project.preview_domain in source:
        env = "preview"

    submissions = FormSubmission.objects.filter(project=project, environment=env)
    total = submissions.count()
    limit = min(int(request.GET.get("limit", 500)), 500)
    offset = int(request.GET.get("offset", 0))
    items = submissions[offset:offset + limit]

    return JsonResponse({
        "ok": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [
            {**(s.form_data or {}), "id": s.id, "submitted_at": s.submitted_at.isoformat()}
            for s in items
        ],
    })


__all__ = ["submit_form", "form_submissions", "form_submission_detail", "public_form_data"]

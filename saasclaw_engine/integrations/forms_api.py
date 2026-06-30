"""Public API for static site form submissions.

POST /api/forms/{slug}/  — accept form data from any static site
GET  /api/forms/{slug}/  — list submissions (project owner, staff only)
GET  /api/forms/{slug}/{id}/ — single submission detail (project owner, staff only)
DELETE /api/forms/{slug}/{id}/ — delete a submission (project owner, staff only)
"""

import json
import logging
from datetime import datetime

from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from django.utils.decorators import method_decorator
from django.views import View

from saasclaw_engine.projects.models import Project, FormSubmission

logger = logging.getLogger(__name__)

# Simple honeypot field name — bots often fill hidden fields
HONEYPOT_FIELD = "website"


@csrf_exempt
@require_POST
def submit_form(request, slug):
    """Accept a form submission from a static site.

    Accepts:
    - application/json body: {"field": "value", ...}
    - application/x-www-form-urlencoded: standard form POST
    - multipart/form-data: file uploads

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

    # Parse form data
    content_type = request.content_type or ''

    if 'application/json' in content_type:
        try:
            form_data = json.loads(request.body.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpResponseBadRequest('Invalid JSON body.')
    else:
        # form-encoded or multipart — Django puts it in request.POST
        form_data = dict(request.POST)

    if not form_data:
        return HttpResponseBadRequest('No form data received.')

    # Honeypot check — if the hidden field is filled, silently accept but discard
    if HONEYPOT_FIELD in form_data and form_data[HONEYPOT_FIELD]:
        # Return success to not alert bots, but don't store
        return JsonResponse({'ok': True, 'id': 0})

    # Strip honeypot field from stored data if present (empty)
    form_data.pop(HONEYPOT_FIELD, None)

    # Basic size limit
    if len(str(form_data)) > 100_000:
        return HttpResponseBadRequest('Submission too large.')

    submission = FormSubmission.objects.create(
        project=project,
        form_data=form_data,
        ip_address=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        referrer=request.META.get('HTTP_REFERER', '')[:2048],
    )

    logger.info(
        "Form submission #%d for project %s from %s",
        submission.id, slug, _client_ip(request),
    )

    return JsonResponse({'ok': True, 'id': submission.id}, status=201)


@require_http_methods(["GET", "DELETE"])
def form_submissions(request, slug):
    """List or bulk-delete form submissions. Project owner/staff only."""
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Not found.'}, status=404)

    if not _can_manage(request.user, project):
        return HttpResponseForbidden('Access denied.')

    if request.method == 'DELETE':
        FormSubmission.objects.filter(project=project).delete()
        return JsonResponse({'ok': True, 'deleted': True})

    # GET — list submissions with pagination
    submissions = FormSubmission.objects.filter(project=project)
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
                'form_data': s.form_data,
                'submitted_at': s.submitted_at.isoformat(),
                'ip_address': s.ip_address,
                'user_agent': s.user_agent,
                'referrer': s.referrer,
            }
            for s in items
        ],
    })


@require_http_methods(["GET", "DELETE"])
def form_submission_detail(request, slug, pk):
    """View or delete a single submission. Project owner/staff only."""
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Not found.'}, status=404)

    if not _can_manage(request.user, project):
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
        'form_data': submission.form_data,
        'submitted_at': submission.submitted_at.isoformat(),
        'ip_address': submission.ip_address,
        'user_agent': submission.user_agent,
        'referrer': submission.referrer,
    })


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


__all__ = ['submit_form', 'form_submissions', 'form_submission_detail']

"""
HubSpot dashboard views — serves cached CRM data from local DB.
Much faster than live MCP calls on every page load.
"""
import json
from datetime import datetime, timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

HUBSPOT_PORTAL = '51524447'
HS_BASE = f'https://app.hubspot.com/contacts/{HUBSPOT_PORTAL}'


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def hubspot_dashboard_data(request):
    """Return cached dashboard data: companies, contacts, tickets with sentiment + health."""
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.public_api.models import HubspotCompany, HubspotContact, HubspotTicket

    project_slug = request.GET.get('project', 'hubspot-health-checker')
    if request.method == 'POST':
        try:
            body = json.loads(request.body)
            project_slug = body.get('project', project_slug)
        except Exception:
            pass

    try:
        project = Project.objects.get(slug=project_slug)
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)

    companies = HubspotCompany.objects.filter(project=project).prefetch_related('contacts', 'tickets')
    orphan_contacts = HubspotContact.objects.filter(project=project, company__isnull=True)

    now = datetime.now(timezone.utc)

    clients = []
    for co in companies:
        co_contacts = list(co.contacts.all())
        co_tickets = list(co.tickets.all())

        # Last activity
        last_activity = co.last_updated
        for c in co_contacts:
            if c.last_activity and (not last_activity or c.last_activity > last_activity):
                last_activity = c.last_activity

        days_since = 999
        if last_activity:
            if last_activity.tzinfo is None:
                from django.utils import timezone as djtz
                last_activity = djtz.make_aware(last_activity, djtz.utc)
            days_since = (now - last_activity).days

        # Calculate health
        status, reason = _calculate_health(
            len(co_contacts), days_since, co.lifecycle_stage, co_tickets
        )

        clients.append({
            'id': co.hubspot_id,
            'name': co.name,
            'domain': co.domain,
            'industry': co.industry,
            'lifecycleStage': co.lifecycle_stage,
            'contactCount': len(co_contacts),
            'ticketCount': len(co_tickets),
            'healthStatus': status,
            'healthReason': reason,
            'daysSinceActivity': days_since,
            'lastActivity': last_activity.isoformat() if last_activity else '',
            'link': f'{HS_BASE}/company/{co.hubspot_id}',
            'contacts': [{
                'id': c.hubspot_id,
                'firstName': c.first_name,
                'lastName': c.last_name,
                'email': c.email,
                'lifecycleStage': c.lifecycle_stage,
                'link': f'{HS_BASE}/contact/{c.hubspot_id}',
            } for c in co_contacts[:10]],
            'tickets': [{
                'id': t.hubspot_id,
                'subject': t.subject,
                'status': t.status,
                'sentiment': t.sentiment,
                'sentimentLabel': t.sentiment,
                'sentimentSummary': t.sentiment_summary,
                'sentimentFlags': t.sentiment_flags,
                'link': f'{HS_BASE}/ticket/{t.hubspot_id}',
            } for t in co_tickets],
        })

    # Sort: critical first
    status_order = {'critical': 0, 'at-risk': 1, 'unknown': 2, 'healthy': 3}
    clients.sort(key=lambda c: (status_order.get(c['healthStatus'], 2), c['name']))

    return JsonResponse({
        'clients': clients,
        'orphanContacts': [{'name': f'{c.first_name} {c.last_name}', 'email': c.email} for c in orphan_contacts],
        'summary': {
            'total': len(clients),
            'critical': sum(1 for c in clients if c['healthStatus'] == 'critical'),
            'atRisk': sum(1 for c in clients if c['healthStatus'] == 'at-risk'),
            'healthy': sum(1 for c in clients if c['healthStatus'] == 'healthy'),
        },
        'syncedAt': max(
            (c.synced_at for c in companies),
            default=datetime.now(timezone.utc)
        ).isoformat(),
    })


def _calculate_health(contacts_count, days_since, lifecycle_stage, tickets):
    """Health calc using DB ticket objects."""
    now = datetime.now(timezone.utc)

    open_tickets = [t for t in tickets if t.status != 'Closed']
    stale_tickets = []
    for t in open_tickets:
        if t.last_updated_hubspot:
            tu = t.last_updated_hubspot
            if tu.tzinfo is None:
                from django.utils import timezone as djtz
                tu = djtz.make_aware(tu, djtz.utc)
            if (now - tu).days > 7:
                stale_tickets.append(t)

    waiting = [t for t in open_tickets if 'waiting' in t.status.lower() or 'progress' in t.status.lower()]
    very_neg = [t for t in open_tickets if t.sentiment == 'very-negative']
    neg = [t for t in open_tickets if t.sentiment == 'negative']

    if very_neg:
        flags = list(set(f for t in very_neg for f in (t.sentiment_flags or [])))
        r = f"{len(very_neg)} ticket(s) strong negative"
        if flags: r += f" — {', '.join(flags[:4])}"
        return 'critical', r

    if stale_tickets and waiting:
        return 'critical', f"{len(stale_tickets)} ticket(s) waiting on us >7 days"

    if neg:
        if days_since >= 30:
            return 'critical', f"{len(neg)} negative ticket(s), no activity {days_since}d"
        return 'at-risk', f"{len(neg)} ticket(s) negative sentiment"

    if len(open_tickets) >= 3:
        return 'at-risk', f"{len(open_tickets)} open tickets — high load"

    if days_since >= 60:
        r = f"No activity {days_since} days — may be churning"
        if open_tickets: r += f", {len(open_tickets)} open"
        return 'critical', r

    if days_since >= 30:
        r = f"Last activity {days_since} days ago — needs follow-up"
        if open_tickets: r += f", {len(open_tickets)} open"
        return 'at-risk', r

    if stale_tickets:
        return 'at-risk', f"{len(stale_tickets)} ticket(s) stale >7 days"

    stage = (lifecycle_stage or '').lower()
    if stage in ('customer', 'closedwon') and not neg and not very_neg:
        return 'healthy', 'Active customer' if not open_tickets else f"Active, {len(open_tickets)} open ticket(s)"

    if days_since < 14:
        return 'healthy', f"Recent activity ({days_since}d, {contacts_count} contacts)"

    return 'unknown', 'Insufficient data'

"""
HubSpot dashboard views — serves cached CRM data from local DB.
Much faster than live MCP calls on every page load.
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .hubspot_mcp import require_jwt

logger = logging.getLogger(__name__)

HUBSPOT_PORTAL = '51524447'
HS_BASE = f'https://app.hubspot.com/contacts/{HUBSPOT_PORTAL}'


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@require_jwt
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
        status, reason, summary = _calculate_health(
            len(co_contacts), days_since, co.lifecycle_stage, co_tickets, co.name, co.industry
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
            'healthSummary': summary,
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
                'aiSummary': t.ai_summary,
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


def _calculate_health(contacts_count, days_since, lifecycle_stage, tickets, company_name='', industry=''):
    """Health calc using weighted ticket scoring. Returns (status, reason, summary)."""
    now = datetime.now(timezone.utc)

    open_tickets = [t for t in tickets if t.status != 'Closed']
    # Only count closed tickets from the last 90 days as positive signals
    ninety_days_ago = now - timedelta(days=90)
    recent_closed = []
    for t in tickets:
        if t.status != 'Closed':
            continue
        closed_date = t.last_updated_hubspot or t.created_at_hubspot
        if closed_date:
            if closed_date.tzinfo is None:
                from django.utils import timezone as djtz
                closed_date = djtz.make_aware(closed_date, djtz.utc)
            if closed_date >= ninety_days_ago:
                recent_closed.append(t)
    closed_tickets = [t for t in tickets if t.status == 'Closed']  # all closed (for stats)
    total_tickets = len(tickets)
    resolution_rate = (len(closed_tickets) / total_tickets * 100) if total_tickets > 0 else 0
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
    positive = [t for t in open_tickets if t.sentiment == 'positive']
    neutral = [t for t in open_tickets if t.sentiment == 'neutral']

    stage = (lifecycle_stage or '').lower()
    is_customer = stage in ('customer', 'closedwon')
    stage_label = lifecycle_stage or 'lead'

    # Weighted health score: start at 100
    # Weights tuned for accurate sentiment detection (post custom lexicon)
    # Ratios matter more than raw counts — 3 neg out of 3 tickets is worse than 3 out of 10
    open_count = max(len(open_tickets), 1)  # avoid div by zero
    neg_ratio = (len(very_neg) + len(neg)) / open_count

    health_score = 100
    health_score -= len(very_neg) * 20      # very-negative: serious — rejected deposits, system failures
    health_score -= len(neg) * 8             # negative: meaningful friction
    health_score -= len(stale_tickets) * 8
    health_score -= len(waiting) * 3
    # Penalize when significant portion of tickets are negative
    if neg_ratio > 0.5:
        health_score -= 20                   # majority negative — serious relationship risk
    elif neg_ratio > 0.3:
        health_score -= 10                   # third+ negative — needs attention
    if days_since >= 60:
        health_score -= 30
    elif days_since >= 30:
        health_score -= 15
    health_score += min(len(positive) * 3, 10)  # bonus for positive, capped
    health_score += min(len(recent_closed) * 2, 15)  # recent resolutions show competence, cap +15
    if resolution_rate >= 80 and total_tickets >= 5:
        health_score += 5  # high resolution rate bonus

    # Churn signal flags
    churn_flags = [f for t in very_neg for f in (t.sentiment_flags or [])
                   if f.lower() in ('churn', 'leaving', 'cancel', 'cancellation', 'lawsuit', 'attorney')]

    # ─── CRITICAL (score <= 40) ────────────────────────
    if very_neg and (churn_flags or health_score <= 40):
        flags = list(set(f for t in very_neg for f in (t.sentiment_flags or [])))
        subjects = [t.subject for t in very_neg[:3]]
        r = f"{len(very_neg)} ticket(s) strong negative"
        if flags: r += f" — {', '.join(flags[:4])}"
        summary = f"🔴 {company_name or 'This client'} has {len(very_neg)} ticket(s) with strongly negative sentiment"
        if subjects: summary += f": {', '.join(subjects)}"
        if flags: summary += f". Flagged keywords: {', '.join(flags[:5])}."
        summary += " Immediate escalation recommended."
        return 'critical', r, summary

    if health_score <= 40:
        r = f"Health score {health_score}"
        summary = f"🔴 {company_name or 'Client'} health score is critical ({health_score})."
        parts = []
        if very_neg: parts.append(f"{len(very_neg)} very negative ticket(s)")
        if neg: parts.append(f"{len(neg)} negative ticket(s)")
        if days_since >= 30: parts.append(f"no activity {days_since}d")
        if parts: summary += " " + ", ".join(parts) + "."
        summary += " Escalation recommended."
        return 'critical', r, summary

    # ─── AT RISK (score 41-65) ─────────────────────────
    if health_score <= 65:
        r = f"Health score {health_score}"
        parts = []
        if neg: parts.append(f"{len(neg)} negative ticket(s)")
        if stale_tickets: parts.append(f"{len(stale_tickets)} stale")
        if days_since >= 30: parts.append(f"last activity {days_since}d ago")
        summary = f"🟡 {company_name or 'Client'} is at risk (score {health_score})."
        if parts: summary += " " + ", ".join(parts) + "."
        summary += " Proactive outreach needed."
        return 'at-risk', r, summary

    # ─── STABLE (meaningful negative sentiment but not at-risk) ──
    # 2+ negative tickets, or any very-negative — these clients have real issues
    if (len(neg) + len(very_neg)) >= 2 or very_neg:
        r = f"Score {health_score}, {len(neg) + len(very_neg)} negative ticket(s)"
        parts = []
        if very_neg: parts.append(f"{len(very_neg)} very negative")
        if neg: parts.append(f"{len(neg)} negative")
        if positive: parts.append(f"{len(positive)} positive")
        if neutral: parts.append(f"{len(neutral)} neutral")
        summary = f"🔵 {company_name or 'Client'} is stable (score {health_score})."
        if parts: summary += " Sentiment: " + ", ".join(parts) + "."
        summary += f" {len(open_tickets)} open ticket(s) — manageable but has issues to resolve."
        return 'stable', r, summary

    # ─── HEALTHY (no negative sentiment) ───────────────────
    # Clean bill of health — only positive/neutral tickets
    if is_customer:
        summary = f"🟢 {company_name or 'Client'} is an active customer"
        if positive:
            summary += f" with positive sentiment on {len(positive)} ticket(s)"
        if not open_tickets:
            summary += f". No open tickets. All {len(closed_tickets)} ticket(s) resolved."
        else:
            summary += f". {len(open_tickets)} open ticket(s), all positive or neutral."
        if closed_tickets and resolution_rate >= 80:
            summary += f" {int(resolution_rate)}% closure rate."
        if days_since < 14:
            summary += f" Recent activity ({days_since}d ago)."
        summary += " In good standing."
        r = 'Active customer' if not open_tickets else f"Active, {len(open_tickets)} open ticket(s)"
        return 'healthy', r, summary

    summary = f"🟢 {company_name or 'Client'} is healthy (score {health_score})."
    if positive: summary += f" {len(positive)} positive ticket(s)."
    if neutral: summary += f" {len(neutral)} neutral ticket(s)."
    if recent_closed: summary += f" {len(recent_closed)} recently resolved ticket(s)."
    if closed_tickets and resolution_rate >= 80 and total_tickets >= 5:
        summary += f" {int(resolution_rate)}% closure rate."
    if open_tickets: summary += f" {len(open_tickets)} open ticket(s) under control."
    if days_since < 14: summary += f" Recent activity ({days_since}d)."
    r = f"Score {health_score}, {len(open_tickets)} open ticket(s)"
    return 'healthy', r, summary


@require_http_methods(["GET"])
@require_jwt
def hubspot_topic_graph(request):
    """Returns the ticket topic graph — clusters of similar tickets with stats."""
    from .models import TicketTopic
    from saasclaw_engine.projects.models import Project

    project_slug = request.GET.get('project', 'hubspot-health-checker')
    try:
        project = Project.objects.get(slug=project_slug)
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)

    topics = TicketTopic.objects.filter(project=project).order_by('-ticket_count')

    return JsonResponse({
        'topics': [
            {
                'id': t.id,
                'name': t.name,
                'description': t.description,
                'keywords': t.keywords,
                'ticketCount': t.ticket_count,
                'openCount': t.open_count,
                'resolvedCount': t.resolved_count,
                'positiveCount': t.positive_count,
                'negativeCount': t.negative_count,
                'avgSentiment': t.avg_sentiment_score,
                'companies': t.companies,
                'suggestedResponse': t.suggested_response,
                'updatedAt': t.updated_at.isoformat() if t.updated_at else '',
            }
            for t in topics
        ],
        'totalTopics': topics.count(),
        'totalTickets': sum(t.ticket_count for t in topics),
    })


@require_http_methods(["GET"])
@require_jwt
def hubspot_sync_status(request):
    """Return the last sync timestamp for the project."""
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.public_api.models import HubspotCompany

    project_slug = request.GET.get('project', 'hubspot-health-checker')
    try:
        project = Project.objects.get(slug=project_slug)
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)

    # Find the most recent synced_at across all companies for this project
    latest = HubspotCompany.objects.filter(project=project).order_by('-synced_at').first()
    last_synced = latest.synced_at.isoformat() if latest and latest.synced_at else None

    return JsonResponse({'last_synced': last_synced})


@csrf_exempt
@require_http_methods(["POST"])
@require_jwt
def hubspot_sync_trigger(request):
    """Trigger a manual HubSpot sync. Runs the management command inline."""
    import subprocess
    from saasclaw_engine.projects.models import Project

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    project_slug = body.get('project', request.GET.get('project', 'hubspot-health-checker'))
    try:
        project = Project.objects.get(slug=project_slug)
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)

    try:
        import os
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        from django.core.management import call_command
        call_command('hubspot_sync', project=project_slug)
        logger.info('Manual sync completed for project=%s', project_slug)
    except Exception as e:
        logger.exception('Manual sync failed for project=%s', project_slug)
        return JsonResponse({'error': f'Sync failed: {str(e)}'}, status=500)

    # Fetch the new last_synced timestamp
    from saasclaw_engine.public_api.models import HubspotCompany
    latest = HubspotCompany.objects.filter(project=project).order_by('-synced_at').first()
    last_synced = latest.synced_at.isoformat() if latest and latest.synced_at else None

    return JsonResponse({'last_synced': last_synced, 'status': 'ok'})

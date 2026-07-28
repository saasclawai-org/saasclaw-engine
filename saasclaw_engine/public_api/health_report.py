"""
HubSpot Health Report — Nightly batch job.

Fetches all companies, contacts, tickets, notes from HubSpot MCP,
runs VADER sentiment analysis on ticket text, calculates health scores,
and writes a report. Called via Django management command.
"""
import json
import urllib.request
import re
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HUBSPOT_PORTAL = '51524447'
HS_BASE = f'https://app.hubspot.com/contacts/{HUBSPOT_PORTAL}'

_analyzer = None

def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _analyzer = SentimentIntensityAnalyzer()
    return _analyzer


def _vader_sentiment(text):
    analyzer = _get_analyzer()
    scores = analyzer.polarity_scores(text)
    compound = scores['compound']
    if compound <= -0.5:
        sentiment, score = 'very-negative', 8
    elif compound < -0.15:
        sentiment, score = 'negative', 4
    elif compound >= 0.4:
        sentiment, score = 'positive', -2
    else:
        sentiment, score = 'neutral', 0
    flags = []
    for w in ['upset','angry','furious','frustrated','unacceptable','broken','cannot','unable','fails','failed','cancel','leaving','leave','wrong','worst','terrible','horrible','awful','useless','unhappy','disappointed','complaint','urgent','escalate']:
        if w in text.lower():
            flags.append(w)
    if sentiment in ('very-negative', 'negative'):
        summary = f'Negative — {", ".join(flags[:3])}' if flags else 'Negative tone'
    elif sentiment == 'positive':
        summary = 'Positive tone'
    else:
        summary = 'Neutral tone'
    return sentiment, score, summary, flags, round(compound, 3)


TICKET_STAGES = {'1': 'New', '2': 'In Progress', '3': 'Waiting on Us', '4': 'Closed', '5': 'Waiting on Customer'}

def _ticket_status(stage_id):
    return TICKET_STAGES.get(str(stage_id), f'Stage {stage_id}')


def calculate_health(contacts_count, days_since, lifecycle_stage, tickets):
    open_tickets = [t for t in tickets if t['status'] != 'Closed']
    stale_tickets = [t for t in open_tickets if t.get('stale', False)]
    waiting = [t for t in open_tickets if 'waiting' in t.get('status','').lower() or 'progress' in t.get('status','').lower()]
    very_neg = [t for t in open_tickets if t.get('sentiment') == 'very-negative']
    if very_neg:
        flags = list(set(f for t in very_neg for f in t.get('flags', [])))
        r = f"{len(very_neg)} ticket(s) strong negative"
        if flags: r += f" — {', '.join(flags[:4])}"
        return 'critical', r
    if stale_tickets and waiting:
        return 'critical', f"{len(stale_tickets)} ticket(s) waiting on us >7 days"
    neg = [t for t in open_tickets if t.get('sentiment') == 'negative']
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
    if stage in ('customer','closedwon') and not neg and not very_neg:
        return 'healthy', 'Active customer' if not open_tickets else f"Active, {len(open_tickets)} open ticket(s)"
    if days_since < 14:
        return 'healthy', f"Recent activity ({days_since}d, {contacts_count} contacts)"
    return 'unknown', 'Insufficient data'


def _mcp_search(project_slug, object_type, properties=None, limit=100, max_pages=10):
    from .models import HubSpotConnection
    from .hubspot_mcp import _refresh_token_if_needed, _mcp_call_tool
    from saasclaw_engine.projects.models import Project
    project = Project.objects.get(slug=project_slug)
    conn = HubSpotConnection.objects.get(project=project)
    token = _refresh_token_if_needed(conn)
    all_results = []
    after = ''
    for _ in range(max_pages):
        args = {'objectType': object_type, 'limit': limit}
        if properties: args['properties'] = properties
        if after: args['after'] = after
        result = _mcp_call_tool(token, 'search_crm_objects', args)
        data = json.loads(result.get('result', result).get('content', [{}])[0].get('text', '{"results":[]}'))
        all_results.extend(data.get('results', []))
        if not data.get('paging', {}).get('next', {}).get('after'): break
        after = data['paging']['next']['after']
    return all_results


def _fetch_association_batch(token, tid):
    """Fetch ticket→company association for a single ticket."""
    url = f'https://api.hubapi.com/crm/v4/objects/tickets/{tid}/associations/companies'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return str(tid), [r['toObjectId'] for r in data.get('results', [])]
    except Exception:
        return str(tid), []


def _get_ticket_associations(project_slug, ticket_ids, batch_size=10):
    """Fetch ticket→company associations in parallel (10 concurrent requests)."""
    from .models import HubSpotConnection
    from .hubspot_mcp import _refresh_token_if_needed
    from saasclaw_engine.projects.models import Project
    project = Project.objects.get(slug=project_slug)
    conn = HubSpotConnection.objects.get(project=project)
    token = _refresh_token_if_needed(conn)
    result = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=batch_size) as executor:
        futures = {executor.submit(_fetch_association_batch, token, tid): tid for tid in ticket_ids}
        for future in as_completed(futures):
            tid, cids = future.result()
            result[tid] = cids
    return result


def _fetch_note_bodies_batch(token, note_ids):
    """Fetch note bodies for a batch of note IDs using HubSpot batch API."""
    bodies = []
    # HubSpot batch read: up to 100 per call
    for i in range(0, len(note_ids), 100):
        chunk = note_ids[i:i+100]
        payload = json.dumps({'inputs': [{'id': str(nid)} for nid in chunk]}).encode()
        url = 'https://api.hubapi.com/crm/v3/objects/notes/batch/read?properties=hs_note_body'
        req = urllib.request.Request(url, data=payload, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                for r in data.get('results', []):
                    raw = r.get('properties', {}).get('hs_note_body', '')
                    clean = re.sub('<[^<]+?>', '', raw).strip()
                    if clean:
                        bodies.append(clean)
        except Exception:
            pass
    return bodies


def _fetch_ticket_notes_single(token, tid):
    """Fetch notes for a single ticket: association lookup + batch note body read."""
    url = f'https://api.hubapi.com/crm/v4/objects/tickets/{tid}/associations/notes'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            note_ids = [r['toObjectId'] for r in data.get('results', [])]
            if not note_ids:
                return str(tid), []
            bodies = _fetch_note_bodies_batch(token, note_ids)
            return str(tid), bodies
    except Exception:
        return str(tid), []


def _get_ticket_notes(project_slug, ticket_ids, batch_size=10):
    """Fetch ticket notes in parallel with batch note body reads."""
    from .models import HubSpotConnection
    from .hubspot_mcp import _refresh_token_if_needed
    from saasclaw_engine.projects.models import Project
    project = Project.objects.get(slug=project_slug)
    conn = HubSpotConnection.objects.get(project=project)
    token = _refresh_token_if_needed(conn)
    result = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=batch_size) as executor:
        futures = {executor.submit(_fetch_ticket_notes_single, token, tid): tid for tid in ticket_ids}
        for future in as_completed(futures):
            tid, bodies = future.result()
            result[tid] = bodies
    return result


def generate_health_report(project_slug='hubspot-health-checker'):
    """Generate report from synced DB data (runs after hubspot_sync)."""
    from saasclaw_engine.projects.models import Project
    from saasclaw_engine.public_api.models import HubspotCompany, HubspotContact, HubspotTicket
    from saasclaw_engine.public_api.hubspot_views import _calculate_health

    now = datetime.now(timezone.utc)
    project = Project.objects.get(slug=project_slug)

    companies = HubspotCompany.objects.filter(project=project).prefetch_related('contacts', 'tickets')
    orphan_contacts = HubspotContact.objects.filter(project=project, company__isnull=True)

    clients = []
    processed_tickets = []

    for co in companies:
        co_contacts = list(co.contacts.all())
        co_tickets = list(co.tickets.all())

        last_activity = co.last_updated
        for c in co_contacts:
            if c.last_activity and (not last_activity or c.last_activity > last_activity):
                last_activity = c.last_activity

        days_since = 999
        if last_activity:
            la = last_activity
            if la.tzinfo is None:
                from django.utils import timezone as djtz
                la = djtz.make_aware(la, djtz.utc)
            days_since = (now - la).days

        status, reason, summary = _calculate_health(
            len(co_contacts), days_since, co.lifecycle_stage, co_tickets, co.name, co.industry
        )

        ticket_data = []
        for t in co_tickets:
            ticket_data.append({
                'id': t.hubspot_id,
                'subject': t.subject,
                'status': t.status,
                'sentiment': t.sentiment,
                'flags': t.sentiment_flags or [],
                'stale': t.last_updated_hubspot and (
                    (now - (t.last_updated_hubspot.replace(tzinfo=timezone.utc) if t.last_updated_hubspot.tzinfo is None
                     else t.last_updated_hubspot)).days > 7
                ),
            })
            processed_tickets.append(ticket_data[-1])

        clients.append({
            'name': co.name,
            'id': co.hubspot_id,
            'status': status,
            'reason': reason,
            'summary': summary,
            'days_since': days_since,
            'tickets': ticket_data,
            'link': f"{HS_BASE}/company/{co.hubspot_id}",
        })

    status_order = {'critical': 0, 'at-risk': 1, 'unknown': 2, 'healthy': 3}
    clients.sort(key=lambda c: (status_order.get(c['status'], 2), c['name']))

    summary = {
        'critical': sum(1 for c in clients if c['status'] == 'critical'),
        'at_risk': sum(1 for c in clients if c['status'] == 'at-risk'),
        'healthy': sum(1 for c in clients if c['status'] == 'healthy'),
        'total_clients': len(clients),
        'total_contacts': HubspotContact.objects.filter(project=project).count(),
        'open_tickets': sum(1 for t in processed_tickets if t['status'] != 'Closed'),
        'negative_tickets': sum(1 for t in processed_tickets if t['sentiment'] in ('negative', 'very-negative')),
        'orphans': orphan_contacts.count(),
    }
    return {
        'generated_at': now.isoformat(),
        'summary': summary,
        'clients': clients,
        'orphan_contacts': [{'name': f'{c.first_name} {c.last_name}'} for c in orphan_contacts],
    }


def format_report_telegram(report):
    s = report['summary']
    lines = [
        "📊 <b>Daily Health Report</b>",
        f"🕐 {report['generated_at'][:16]} UTC",
        "",
        f"🏢 {s['total_clients']} clients | 👥 {s['total_contacts']} contacts | 🎫 {s['open_tickets']} open tickets",
        f"🔴 {s['critical']} critical | 🟡 {s['at_risk']} at-risk | 🟢 {s['healthy']} healthy",
        "",
    ]
    for c in report['clients']:
        emoji = {'critical': '🔴', 'at-risk': '🟡', 'healthy': '🟢', 'unknown': '⚪'}.get(c['status'], '⚪')
        lines.append(f"{emoji} <b>{c['name']}</b>")
        lines.append(f"   {c['reason']}")
        if c.get('summary'):
            # Strip emoji prefix from summary if present, keep it concise
            s = c['summary']
            lines.append(f"   <i>{s[:200]}</i>")
        neg = [t for t in c.get('tickets',[]) if t.get('sentiment') in ('negative','very-negative')]
        if neg:
            lines.append(f"   😡 {len(neg)} negative: {', '.join(t['subject'] for t in neg[:3])}")
        lines.append("")
    if report['orphan_contacts']:
        lines.append(f"👤 {len(report['orphan_contacts'])} contact(s) without company")
    return '\n'.join(lines)

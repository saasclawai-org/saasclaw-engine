"""
Topic graph extraction from HubSpot ticket data.

Clusters tickets by keyword similarity, generates:
- Topic names + keywords
- Per-topic stats (count, sentiment breakdown, companies)
- LLM-generated suggested responses per topic

Designed to run nightly as part of the sync pipeline.
"""
import re
from collections import Counter, defaultdict
from django.db import transaction

from .models import HubspotTicket, TicketTopic

# ─── Stopwords ──────────────────────────────────────────────────
STOPWORDS = frozenset("""
a an the and or but in on at to for of with from by be is are was were been being
have has had do does did will would could should may might must shall can need this
that these those it its their our your his her my we us you they them he she him
not no nor so than too very just about above below up down out off over under again
further once here there all any both each few more most other some such only own same
don t now help please issue question how what when where why
thanks thank you regarding unable getting trying want let us know see get got
""".split())

# ─── Topic keyword patterns ────────────────────────────────────
TOPIC_RULES = [
    {
        'name': 'Payroll Processing',
        'keywords': ['payroll', 'off-cycle', 'paycheck', 'pay period', 'direct deposit', 'gross', 'net pay',
                     'earning', 'deduction', 'garnishment', 'tax', 'w-2', 'w2', 'run payroll', 'pay run'],
        'description': 'Questions about running payroll, pay periods, earnings, and deductions',
    },
    {
        'name': 'Time & Attendance',
        'keywords': ['time clock', 'timeclock', 'clock in', 'clock out', 'punch', 'rounding', 'timesheet',
                     'hours', 'overtime', 'attendance', 'mobile clock', 'time tracking'],
        'description': 'Time clocks, timesheets, hours tracking, and overtime',
    },
    {
        'name': 'Onboarding & Forms',
        'keywords': ['onboarding', 'onboard', 'new hire', 'i-9', 'i9', 'w-4', 'w4', 'new employee',
                     'orientation', 'forms', 'assign forms', 'by location'],
        'description': 'Employee onboarding, forms, and new hire setup',
    },
    {
        'name': 'Access & Administration',
        'keywords': ['access', 'administrator', 'admin', 'login', 'log in', 'sign in', 'password',
                     'permission', 'role', 'account', 'cannot login', 'locked out', 'new administrator',
                     'user account', 'provisioning'],
        'description': 'User access, permissions, admin accounts, and login issues',
    },
    {
        'name': 'Reporting & Analytics',
        'keywords': ['report', 'reporting', 'analytics', 'export', 'dashboard', 'summary',
                     'department', 'quarter', 'data', 'spreadsheet', 'csv'],
        'description': 'Reports, exports, analytics, and data extraction',
    },
    {
        'name': 'Benefits & Enrollment',
        'keywords': ['benefit', 'benefits', 'enrollment', 'insurance', '401k',
                     'health', 'dental', 'vision', 'premium'],
        'description': 'Benefits administration, enrollment, and deductions',
    },
    {
        'name': 'Support Experience',
        'keywords': ['support', 'account manager', 'response time', 'waiting', 'follow up',
                     'update', 'escalat', 'frustrated', 'unresolved', 'no response', 'excellent',
                     'great service', 'thank you', 'responsive'],
        'description': 'Feedback on support quality, response times, and service experience',
    },
    {
        'name': 'Compliance & Legal',
        'keywords': ['compliance', 'legal', 'lawsuit', 'attorney', 'flsa', 'labor law',
                     'regulation', 'audit', 'department of labor', 'dol'],
        'description': 'Compliance, legal matters, and regulatory questions',
    },
    {
        'name': 'Integrations',
        'keywords': ['integration', 'api', 'sync', 'quickbooks', 'qbo', 'accounting',
                     'webhook', 'connection', 'export to', 'import'],
        'description': 'Third-party integrations, API connections, and data sync',
    },
    {
        'name': 'Training & Documentation',
        'keywords': ['training', 'documentation', 'guide', 'tutorial', 'how do we',
                     'learn', 'walkthrough', 'instructions', 'steps'],
        'description': 'Training requests, documentation, and how-to guidance',
    },
]


def _tokenize(text):
    """Split text into lowercase tokens."""
    return re.findall(r'[a-z0-9]+', text.lower())


def _classify_ticket(ticket_text):
    """Return (topic_name, matched_keywords) for a ticket."""
    text_lower = ticket_text.lower()
    tokens = set(_tokenize(text_lower))
    bigrams = set()
    token_list = _tokenize(text_lower)
    for i in range(len(token_list) - 1):
        bigrams.add(f'{token_list[i]} {token_list[i+1]}')

    best_topic = None
    best_score = 0
    best_keywords = []

    for rule in TOPIC_RULES:
        matched = []
        score = 0
        for kw in rule['keywords']:
            if ' ' in kw:
                if kw in bigrams or kw in text_lower:
                    matched.append(kw)
                    score += 2
            else:
                if kw in tokens:
                    matched.append(kw)
                    score += 1
        if score > best_score:
            best_score = score
            best_topic = rule['name']
            best_keywords = matched

    return best_topic, best_keywords


def _extract_keywords(text, max_keywords=8):
    """Extract top keywords using simple TF."""
    tokens = _tokenize(text)
    filtered = [t for t in tokens if t not in STOPWORDS and len(t) > 2]
    if not filtered:
        return []
    freq = Counter(filtered)
    return [kw for kw, _ in freq.most_common(max_keywords)]


def _generate_suggested_response(topic_name, sample_tickets):
    """Generate a suggested response from topic + resolved tickets."""
    resolved = [t for t in sample_tickets if t.status == 'Closed']
    if resolved:
        with_summary = [t for t in resolved if t.ai_summary]
        if with_summary:
            sample = with_summary[0]
            return (f"Based on {len(resolved)} resolved ticket(s) with similar "
                    f"'{topic_name}' issues: {sample.ai_summary}")

    templates = {
        'Payroll Processing': 'To process payroll: Navigate to Payroll > Run Payroll > Select pay period > Review earnings/deductions > Submit. For off-cycle: Payroll > Off-Cycle Run > Select employee and payment type.',
        'Time & Attendance': 'For time clock configuration: Settings > Time Tracking > Clock Rules. Adjust rounding and punch tolerance. Mobile clock requires the mobile app with location enabled.',
        'Onboarding & Forms': 'Onboarding forms can be assigned by location, department, or role: Onboarding > Templates > Assign Rules to set conditional form assignment.',
        'Access & Administration': 'To add an administrator: Settings > Users > Add User > Assign Role > Set permissions. The user receives an email with setup instructions.',
        'Reporting & Analytics': 'Custom reports: Reports > New Report > Select data source > Group by department/date > Add columns. Export to CSV or schedule for auto-delivery.',
        'Support Experience': 'Thank you for the feedback. We aim to respond to all tickets within one business day. Escalations go to the account manager and then to engineering.',
    }
    return templates.get(topic_name, f'Review similar tickets and topic details for guidance on {topic_name.lower()}.')


def build_topic_graph(project):
    """
    Build/update the topic graph from all tickets.
    Returns stats dict.
    """
    tickets = list(HubspotTicket.objects.filter(project=project).select_related('company'))

    if not tickets:
        return {'topics_created': 0, 'tickets_classified': 0}

    ticket_topics = {}
    topic_tickets = defaultdict(list)

    for ticket in tickets:
        text = ' '.join([
            ticket.subject or '',
            ticket.description or '',
            ' '.join(ticket.notes or []),
            ticket.ai_summary or '',
        ])
        topic_name, keywords = _classify_ticket(text)

        if not topic_name:
            kws = _extract_keywords(text)
            topic_name = 'Other' if kws else 'Uncategorized'
            keywords = kws

        ticket_topics[ticket.id] = (topic_name, keywords)
        topic_tickets[topic_name].append(ticket)

    topics_created = 0
    with transaction.atomic():
        TicketTopic.objects.filter(project=project).delete()

        for topic_name, topic_tickets_list in topic_tickets.items():
            description = ''
            for rule in TOPIC_RULES:
                if rule['name'] == topic_name:
                    description = rule['description']
                    break

            all_keywords = set()
            for _, kws in [ticket_topics[t.id] for t in topic_tickets_list]:
                all_keywords.update(kws)

            total = len(topic_tickets_list)
            open_count = sum(1 for t in topic_tickets_list if t.status != 'Closed')
            resolved_count = total - open_count
            positive = sum(1 for t in topic_tickets_list if t.sentiment == 'positive')
            negative = sum(1 for t in topic_tickets_list if t.sentiment in ('negative', 'very-negative'))
            avg_score = sum(t.sentiment_score for t in topic_tickets_list) / total if total else 0

            company_names = list(set(
                t.company.name for t in topic_tickets_list
                if t.company and t.company.name
            ))

            suggested = _generate_suggested_response(topic_name, topic_tickets_list)

            topic = TicketTopic.objects.create(
                project=project,
                name=topic_name,
                keywords=sorted(all_keywords)[:15],
                description=description,
                ticket_count=total,
                open_count=open_count,
                resolved_count=resolved_count,
                positive_count=positive,
                negative_count=negative,
                avg_sentiment_score=round(avg_score, 2),
                companies=company_names,
                suggested_response=suggested,
            )

            for t in topic_tickets_list:
                HubspotTicket.objects.filter(id=t.id).update(topic=topic)

            topics_created += 1

    return {
        'topics_created': topics_created,
        'tickets_classified': len(tickets),
        'topics': {name: len(tix) for name, tix in topic_tickets.items()},
    }

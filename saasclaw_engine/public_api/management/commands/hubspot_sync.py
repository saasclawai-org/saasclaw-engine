"""
Django management command: hubspot_sync

Syncs companies, contacts, and tickets from HubSpot MCP to local DB.
Runs nightly before the health report.

Incremental sync: only fetches associations/notes/sentiment for tickets
that are new or modified since last sync. Unchanged tickets are skipped.

Usage:
    python manage.py hubspot_sync --project hubspot-health-checker
"""
import json
import urllib.request
import re
import logging
from datetime import datetime, timezone
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

TICKET_STAGES = {'1': 'New', '2': 'In Progress', '3': 'Waiting on Us', '4': 'Closed', '5': 'Waiting on Customer'}


class Command(BaseCommand):
    help = 'Sync HubSpot CRM data to local database (incremental)'

    def add_arguments(self, parser):
        parser.add_argument('--project', default='hubspot-health-checker', help='Project slug')

    def handle(self, *args, **options):
        from saasclaw_engine.public_api.health_report import (
            _mcp_search, _get_ticket_associations, _get_ticket_notes, _vader_sentiment
        )
        from saasclaw_engine.public_api.models import (
            HubspotCompany, HubspotContact, HubspotTicket
        )
        from saasclaw_engine.projects.models import Project

        project_slug = options['project']
        self.stdout.write(f'Syncing HubSpot data for {project_slug}...')

        project = Project.objects.get(slug=project_slug)

        # ─── Sync Companies ───────────────────────────────
        companies_raw = _mcp_search(project_slug, 'companies', [
            'name', 'domain', 'industry', 'lifecyclestage', 'hs_lastmodifieddate'
        ])
        company_id_map = {}  # hubspot_id -> HubspotCompany

        for co in companies_raw:
            props = co.get('properties', {})
            hs_id = str(co['id'])
            updated_str = props.get('hs_lastmodifieddate', '')
            last_updated = self._parse_dt(updated_str)

            obj, created = HubspotCompany.objects.update_or_create(
                project=project, hubspot_id=hs_id,
                defaults={
                    'name': props.get('name', ''),
                    'domain': props.get('domain', ''),
                    'industry': props.get('industry', ''),
                    'lifecycle_stage': props.get('lifecyclestage', 'lead'),
                    'last_updated': last_updated,
                }
            )
            company_id_map[hs_id] = obj

        self.stdout.write(f'  Companies: {len(companies_raw)} synced')

        # Build name lookup for contacts
        company_by_name = {v.name.lower(): v for v in company_id_map.values()}

        # ─── Sync Contacts ────────────────────────────────
        contacts_raw = _mcp_search(project_slug, 'contacts', [
            'firstname', 'lastname', 'email', 'company', 'lifecyclestage',
            'createdate', 'hs_lastmodifieddate', 'lastactivitydate'
        ])

        for c in contacts_raw:
            props = c.get('properties', {})
            hs_id = str(c['id'])
            company_name = props.get('company', '')
            company = company_by_name.get(company_name.lower()) if company_name else None
            last_activity = self._parse_dt(props.get('lastactivitydate', '') or props.get('hs_lastmodifieddate', ''))

            HubspotContact.objects.update_or_create(
                project=project, hubspot_id=hs_id,
                defaults={
                    'first_name': props.get('firstname', ''),
                    'last_name': props.get('lastname', ''),
                    'email': props.get('email', ''),
                    'company_name': company_name,
                    'company': company,
                    'lifecycle_stage': props.get('lifecyclestage', 'lead'),
                    'last_activity': last_activity,
                }
            )

        self.stdout.write(f'  Contacts: {len(contacts_raw)} synced')

        # ─── Sync Tickets (incremental) ───────────────────
        tickets_raw = _mcp_search(project_slug, 'tickets', [
            'subject', 'content', 'hs_pipeline_stage', 'hs_ticket_priority',
            'createdate', 'hs_lastmodifieddate'
        ])

        # Build map of existing tickets to detect changes
        existing_tickets = {}
        for t in HubspotTicket.objects.filter(project=project):
            existing_tickets[t.hubspot_id] = t

        # Determine which tickets are new or changed
        changed_ids = []
        unchanged_count = 0
        for t in tickets_raw:
            hs_id = str(t['id'])
            props = t.get('properties', {})
            updated_str = props.get('hs_lastmodifieddate', '')
            hs_updated = self._parse_dt(updated_str)

            existing = existing_tickets.get(hs_id)
            if existing and existing.last_updated_hubspot and hs_updated:
                if existing.last_updated_hubspot >= hs_updated:
                    unchanged_count += 1
                    continue  # Skip — no changes

            changed_ids.append(hs_id)

        self.stdout.write(f'  Tickets: {len(tickets_raw)} total, {len(changed_ids)} new/changed, {unchanged_count} unchanged (skipped)')

        # Only fetch associations + notes + sentiment for changed tickets
        if changed_ids:
            associations = _get_ticket_associations(project_slug, changed_ids)
            ticket_notes = _get_ticket_notes(project_slug, changed_ids)
            self.stdout.write(f'  Fetched associations + notes for {len(changed_ids)} tickets')
        else:
            associations = {}
            ticket_notes = {}
            self.stdout.write(f'  No changed tickets — skipped association/note fetch entirely')

        now = datetime.now(timezone.utc)

        for t in tickets_raw:
            props = t.get('properties', {})
            hs_id = str(t['id'])

            stage = props.get('hs_pipeline_stage', '')
            status = TICKET_STAGES.get(str(stage), f'Stage {stage}')
            created_str = props.get('createdate', '')
            updated_str = props.get('hs_lastmodifieddate', '')

            if hs_id in changed_ids:
                # Full processing: sentiment + notes + associations
                notes = ticket_notes.get(hs_id, [])
                full_text = ' '.join([props.get('subject', ''), props.get('content', ''), *notes])
                sentiment, sent_score, sent_summary, flags, compound = _vader_sentiment(full_text)

                company_ids = associations.get(hs_id, [])
                company = company_id_map.get(str(company_ids[0])) if company_ids else None

                HubspotTicket.objects.update_or_create(
                    project=project, hubspot_id=hs_id,
                    defaults={
                        'subject': props.get('subject', '(no subject)'),
                        'description': props.get('content', ''),
                        'status': status,
                        'pipeline_stage': str(stage),
                        'company': company,
                        'sentiment': sentiment,
                        'sentiment_score': sent_score,
                        'sentiment_summary': sent_summary,
                        'sentiment_flags': flags,
                        'notes': notes,
                        'created_at_hubspot': self._parse_dt(created_str),
                        'last_updated_hubspot': self._parse_dt(updated_str),
                    }
                )
            else:
                # Unchanged: just update company FK + status (in case company was just linked)
                existing = existing_tickets.get(hs_id)
                if existing:
                    # Update status/pipeline in case stage changed without lastmodifieddate bump
                    existing.status = status
                    existing.pipeline_stage = str(stage)
                    existing.last_updated_hubspot = self._parse_dt(updated_str)
                    existing.save(update_fields=['status', 'pipeline_stage', 'last_updated_hubspot'])

        # Re-link company FKs for unchanged tickets (company may have been added since)
        for t in tickets_raw:
            hs_id = str(t['id'])
            existing = existing_tickets.get(hs_id)
            if existing and hs_id not in changed_ids and not existing.company:
                # Try to find company via name match as fallback
                pass  # associations only fetched for changed tickets

        self.stdout.write(f'  Tickets: {len(tickets_raw)} processed ({len(changed_ids)} full, {unchanged_count} light)')

        # ─── Prune deleted records ────────────────────────
        hs_company_ids = {str(c['id']) for c in companies_raw}
        hs_contact_ids = {str(c['id']) for c in contacts_raw}
        hs_ticket_ids = {str(t['id']) for t in tickets_raw}

        deleted_co = HubspotCompany.objects.filter(project=project).exclude(hubspot_id__in=hs_company_ids).delete()[0]
        deleted_ct = HubspotContact.objects.filter(project=project).exclude(hubspot_id__in=hs_contact_ids).delete()[0]
        deleted_tk = HubspotTicket.objects.filter(project=project).exclude(hubspot_id__in=hs_ticket_ids).delete()[0]

        if deleted_co or deleted_ct or deleted_tk:
            self.stdout.write(f'  Pruned: {deleted_co} companies, {deleted_ct} contacts, {deleted_tk} tickets (deleted in HubSpot)')

        self.stdout.write(self.style.SUCCESS('Sync complete'))

    def _parse_dt(self, dt_str):
        if not dt_str:
            return None
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

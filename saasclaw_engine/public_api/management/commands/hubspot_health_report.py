"""
Django management command: hubspot_health_report

Runs nightly health analysis and writes report to file.
Scheduled via system cron.

Usage:
    python manage.py hubspot_health_report --project hubspot-health-checker
"""
import os
import logging
from datetime import datetime, timezone
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate HubSpot health report for all clients'

    def add_arguments(self, parser):
        parser.add_argument('--project', default='hubspot-health-checker', help='Project slug')
        parser.add_argument('--output', default='/srv/saasclaw/projects/hubspot-health-checker/runtime/health-reports', help='Output directory')

    def handle(self, *args, **options):
        from saasclaw_engine.public_api.health_report import generate_health_report, format_report_telegram

        project_slug = options['project']
        output_dir = options['output']

        self.stdout.write(f'Generating health report for {project_slug}...')

        try:
            report = generate_health_report(project_slug)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Report generation failed: {e}'))
            logger.exception('Health report generation failed')
            return

        os.makedirs(output_dir, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # JSON
        json_path = os.path.join(output_dir, f'health-{date_str}.json')
        import json
        with open(json_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        # Telegram format
        tg_path = os.path.join(output_dir, f'health-{date_str}.txt')
        tg_text = format_report_telegram(report)
        with open(tg_path, 'w') as f:
            f.write(tg_text)

        s = report['summary']
        self.stdout.write(self.style.SUCCESS(
            f'Report: {s["total_clients"]} clients, '
            f'{s["critical"]} critical, {s["at_risk"]} at-risk, {s["healthy"]} healthy'
        ))

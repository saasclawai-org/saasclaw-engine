"""Import tax data from states.ts and federal.ts into the database.

Uses tsx to evaluate TypeScript files and output JSON.
"""
import json
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from saasclaw_engine.tax_data.models import (
    FederalTaxYear, FederalBracket,
    StateTaxProfile, StateBracket, StateInsuranceRate,
)


EXTRACT_SCRIPT = '''
import { TAX_YEARS } from './src/data/federal.ts';
import { STATE_TAX_DATA, STATE_INSURANCE } from './src/data/states.ts';

// Deep convert to plain JSON (strip undefined values)
function deepClean(obj) {
  if (obj === null || obj === undefined) return undefined;
  if (Array.isArray(obj)) return obj.map(deepClean).filter(v => v !== undefined);
  if (typeof obj === 'object') {
    const result = {};
    for (const [k, v] of Object.entries(obj)) {
      const cleaned = deepClean(v);
      if (cleaned !== undefined) result[k] = cleaned;
    }
    return result;
  }
  if (typeof obj === 'number' && isNaN(obj)) return undefined;
  return obj;
}

console.log(JSON.stringify({
  federal: deepClean(TAX_YEARS),
  states: { STATE_TAX_DATA: deepClean(STATE_TAX_DATA), STATE_INSURANCE: deepClean(STATE_INSURANCE) }
}));
'''


class Command(BaseCommand):
    help = 'Import tax data from states.ts and federal.ts into the database'

    def add_arguments(self, parser):
        parser.add_argument('--year', type=int, default=2026, help='Tax year to import (default: 2026)')
        parser.add_argument('--force', action='store_true', help='Overwrite existing data for the year')

    def handle(self, *args, **options):
        year = options['year']
        force = options['force']

        project_dir = '/srv/saasclaw/projects/paycheck-calculator'
        script_path = f'{project_dir}/_extract_tax.mjs'

        # Write the extraction script
        Path(script_path).write_text(EXTRACT_SCRIPT)

        try:
            result = subprocess.run(
                ['npx', 'tsx', script_path],
                capture_output=True, text=True, timeout=60,
                cwd=project_dir,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)

        if result.returncode != 0:
            self.stderr.write(f'Extraction error: {result.stderr[:500]}')
            sys.exit(1)

        # Parse the JSON output
        data = json.loads(result.stdout)

        with transaction.atomic():
            self._import_federal(year, data['federal'], force)
            self._import_states(year, data['states'], force)

        self.stdout.write(self.style.SUCCESS(f'Import complete for tax year {year}'))

    def _import_federal(self, year, federal_data, force):
        """Import federal tax year data from parsed JSON."""
        year_data = federal_data.get(str(year)) or federal_data.get(year)
        if not year_data:
            self.stderr.write(f'No federal data found for year {year}')
            return

        existing = FederalTaxYear.objects.filter(year=year).first()
        if existing and not force:
            self.stdout.write(f'Federal {year} already exists, skipping (use --force to overwrite)')
            return
        if existing:
            existing.brackets.all().delete()
            existing.delete()

        fica = year_data.get('fica', {})
        std = year_data.get('standardDeduction', {})
        thresholds = fica.get('additional_medicare_threshold', {})

        # Extract Pub 15-T deduction equivalents from Step 2 Checkbox brackets
        # The 0% bracket max in step2Checkbox equals the line_1g deduction equivalent
        step2 = year_data.get('withholding', {}).get('step2Checkbox', {})
        pub15t_single = 8600
        pub15t_married = 12900
        step2_single = step2.get('single_or_mfs', [])
        step2_mfj = step2.get('married_filing_jointly', [])
        if step2_single and step2_single[0].get('max') is not None:
            pub15t_single = step2_single[0]['max']
        if step2_mfj and step2_mfj[0].get('max') is not None:
            pub15t_married = step2_mfj[0]['max']

        tax_year = FederalTaxYear.objects.create(
            year=year,
            is_active=True,
            social_security_rate=float(fica.get('social_security_rate', 0.062)),
            social_security_wage_base=int(fica.get('social_security_wage_base', 177000)),
            medicare_rate=float(fica.get('medicare_rate', 0.0145)),
            additional_medicare_rate=float(fica.get('additional_medicare_rate', 0.009)),
            additional_medicare_threshold_single=int(thresholds.get('single', 200000)),
            additional_medicare_threshold_mfj=int(thresholds.get('married_filing_jointly', 250000)),
            additional_medicare_threshold_mfs=int(thresholds.get('married_filing_separately', 125000)),
            additional_medicare_threshold_hoh=int(thresholds.get('head_of_household', 200000)),
            standard_deduction_single=int(std.get('single', 15000)),
            standard_deduction_married=int(std.get('married_filing_jointly', 30000)),
            standard_deduction_hoh=int(std.get('head_of_household', 22500)),
            pub15t_deduction_single=pub15t_single,
            pub15t_deduction_married=pub15t_married,
            note=year_data.get('note', ''),
        )

        bracket_count = 0
        schedule_map = {
            'brackets': {
                'single': 'income_single',
                'married_filing_jointly': 'income_married',
                'head_of_household': 'income_hoh',
            },
        }
        # Withholding schedules
        for ws_type in ['standard', 'step2Checkbox']:
            ws_data = year_data.get('withholding', {}).get(ws_type, {})
            prefix = 'withholding_standard' if ws_type == 'standard' else 'withholding_step2'
            for ts_key, db_suffix in [('single_or_mfs', 'single'), ('married_filing_jointly', 'married'), ('head_of_household', 'hoh')]:
                schedule_map_key = f'{ws_type}:{ts_key}'
                schedule_map[schedule_map_key] = f'{prefix}_{db_suffix}'

        # Import all brackets
        for schedule_source in ['brackets', 'withholding']:
            if schedule_source == 'brackets':
                for ts_key, db_schedule in schedule_map['brackets'].items():
                    for b in year_data.get('brackets', {}).get(ts_key, []):
                        FederalBracket.objects.create(
                            tax_year=tax_year,
                            schedule=db_schedule,
                            min_amount=int(b.get('min', 0)),
                            max_amount=int(b['max']) if b.get('max') is not None else None,
                            rate=float(b.get('rate', 0)),
                        )
                        bracket_count += 1
            else:
                for ws_type in ['standard', 'step2Checkbox']:
                    ws_data = year_data.get('withholding', {}).get(ws_type, {})
                    prefix = 'withholding_standard' if ws_type == 'standard' else 'withholding_step2'
                    for ts_key, db_suffix in [('single_or_mfs', 'single'), ('married_filing_jointly', 'married'), ('head_of_household', 'hoh')]:
                        db_schedule = f'{prefix}_{db_suffix}'
                        for b in ws_data.get(ts_key, []):
                            FederalBracket.objects.create(
                                tax_year=tax_year,
                                schedule=db_schedule,
                                min_amount=int(b.get('min', 0)),
                                max_amount=int(b['max']) if b.get('max') is not None else None,
                                rate=float(b.get('rate', 0)),
                            )
                            bracket_count += 1

        # Deactivate other years
        FederalTaxYear.objects.exclude(pk=tax_year.pk).update(is_active=False)

        self.stdout.write(f'  Federal {year}: {bracket_count} brackets imported')

    def _import_states(self, year, states_data, force):
        """Import state tax profiles from parsed JSON."""
        state_tax_data = states_data.get('STATE_TAX_DATA', {})
        state_insurance = states_data.get('STATE_INSURANCE', {})

        count = 0
        for code, config in state_tax_data.items():
            code = code.upper()
            if len(code) != 2:
                continue

            existing = StateTaxProfile.objects.filter(year=year, state_code=code).first()
            if existing and not force:
                continue
            if existing:
                existing.brackets.all().delete()
                existing.insurance_rates.all().delete()
                existing.delete()

            std_ded = config.get('standardDeduction', {})
            per_exempt = config.get('personalExemption', {})

            profile = StateTaxProfile.objects.create(
                year=year,
                state_code=code,
                state_name=config.get('name', code),
                tax_type=config.get('type', 'progressive'),
                flat_rate=config.get('flatRate'),
                standard_deduction_single=int(std_ded.get('single', 0)) if isinstance(std_ded, dict) else 0,
                standard_deduction_married=int(std_ded.get('married', 0)) if isinstance(std_ded, dict) else 0,
                standard_deduction_hoh=int(std_ded.get('head_of_household', 0)) if isinstance(std_ded, dict) else 0,
                personal_exemption_single=int(per_exempt.get('single', 0)) if isinstance(per_exempt, dict) else 0,
                personal_exemption_married=int(per_exempt.get('married', 0)) if isinstance(per_exempt, dict) else 0,
                personal_exemption_hoh=int(per_exempt.get('head_of_household', 0)) if isinstance(per_exempt, dict) else 0,
                dependent_exemption=int(config.get('dependentExemption', 0) or 0),
                has_local_taxes=bool(config.get('hasLocalTaxes', False)),
                local_tax_note=str(config.get('localTaxNote', '')),
            )

            # Brackets
            brackets = config.get('brackets', {})
            if isinstance(brackets, dict):
                status_map = {'single': 'single', 'married': 'married', 'head_of_household': 'head_of_household'}
                for filing_status, bracket_list in brackets.items():
                    fs = status_map.get(filing_status, filing_status)
                    for b in bracket_list:
                        StateBracket.objects.create(
                            profile=profile,
                            filing_status=fs,
                            min_amount=int(b.get('min', 0)),
                            max_amount=int(b['max']) if b.get('max') is not None else None,
                            rate=float(b.get('rate', 0)),
                        )

            # Insurance rates
            ins = state_insurance.get(code, {})
            if isinstance(ins, dict):
                for sdi in (ins.get('sdi') or []):
                    StateInsuranceRate.objects.create(
                        profile=profile, category='sdi',
                        name=sdi.get('name', f'{code} SDI'),
                        rate=float(sdi.get('rate', 0)),
                        wage_base=int(sdi['wageBase']) if sdi.get('wageBase') else None,
                    )
                for pfml in (ins.get('pfml') or []):
                    StateInsuranceRate.objects.create(
                        profile=profile, category='pfml',
                        name=pfml.get('name', f'{code} PFML'),
                        rate=float(pfml.get('rate', 0)),
                        wage_base=int(pfml['wageBase']) if pfml.get('wageBase') else None,
                    )
                sui = ins.get('sui')
                if sui:
                    StateInsuranceRate.objects.create(
                        profile=profile, category='sui',
                        name=sui.get('name', f'{code} SUI'),
                        rate=float(sui.get('rate', 0)),
                        wage_base=int(sui['wageBase']) if sui.get('wageBase') else None,
                    )
            count += 1

        self.stdout.write(f'  States: {count} profiles imported for {year}')
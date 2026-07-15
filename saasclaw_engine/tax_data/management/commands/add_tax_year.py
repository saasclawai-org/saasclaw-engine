"""Add a new tax year by cloning from an existing year."""
from django.core.management.base import BaseCommand
from saasclaw_engine.tax_data.models import FederalTaxYear, FederalBracket, StateTaxProfile, StateBracket, StateInsuranceRate
from decimal import Decimal


class Command(BaseCommand):
    help = 'Clone tax data from one year to create a new year'

    def add_arguments(self, parser):
        parser.add_argument('source_year', type=int, help='Year to clone from')
        parser.add_argument('target_year', type=int, help='Year to create')

    def handle(self, *args, **options):
        source_year = options['source_year']
        target_year = options['target_year']

        # Clone federal tax year
        try:
            source_fy = FederalTaxYear.objects.get(year=source_year)
        except FederalTaxYear.DoesNotExist:
            self.stderr.write(f'No federal data for {source_year}')
            return

        target_fy, created = FederalTaxYear.objects.get_or_create(
            year=target_year,
            defaults={
                'is_active': False,
                'social_security_rate': source_fy.social_security_rate,
                'social_security_wage_base': source_fy.social_security_wage_base,
                'medicare_rate': source_fy.medicare_rate,
                'additional_medicare_rate': source_fy.additional_medicare_rate,
                'additional_medicare_threshold_single': source_fy.additional_medicare_threshold_single,
                'additional_medicare_threshold_mfj': source_fy.additional_medicare_threshold_mfj,
                'additional_medicare_threshold_mfs': source_fy.additional_medicare_threshold_mfs,
                'additional_medicare_threshold_hoh': source_fy.additional_medicare_threshold_hoh,
                'standard_deduction_single': source_fy.standard_deduction_single,
                'standard_deduction_married': source_fy.standard_deduction_married,
                'standard_deduction_hoh': source_fy.standard_deduction_hoh,
                'pub15t_deduction_single': source_fy.pub15t_deduction_single,
                'pub15t_deduction_married': source_fy.pub15t_deduction_married,
                'note': f'Cloned from {source_year} — review and update for {target_year}',
            }
        )
        if created:
            # Clone brackets
            bracket_count = 0
            for b in source_fy.brackets.all():
                FederalBracket.objects.create(
                    tax_year=target_fy,
                    schedule=b.schedule,
                    min_amount=b.min_amount,
                    max_amount=b.max_amount,
                    rate=b.rate,
                )
                bracket_count += 1
            self.stdout.write(f'Created FederalTaxYear {target_year} with {bracket_count} brackets')
        else:
            self.stdout.write(f'FederalTaxYear {target_year} already exists')

        # Clone state profiles
        states_copied = 0
        for sp in StateTaxProfile.objects.filter(year=source_year):
            new_sp, created = StateTaxProfile.objects.get_or_create(
                year=target_year,
                state_code=sp.state_code,
                defaults={
                    'state_name': sp.state_name,
                    'tax_type': sp.tax_type,
                    'flat_rate': sp.flat_rate,
                    'standard_deduction_single': sp.standard_deduction_single,
                    'standard_deduction_married': sp.standard_deduction_married,
                    'standard_deduction_hoh': sp.standard_deduction_hoh,
                    'personal_exemption_single': sp.personal_exemption_single,
                    'personal_exemption_married': sp.personal_exemption_married,
                    'personal_exemption_hoh': sp.personal_exemption_hoh,
                    'dependent_exemption': sp.dependent_exemption,
                    'withholding_method': sp.withholding_method,
                    'withholding_allowance_single': sp.withholding_allowance_single,
                    'withholding_allowance_married': sp.withholding_allowance_married,
                    'withholding_allowance_hoh': sp.withholding_allowance_hoh,
                    'default_allowances_single': sp.default_allowances_single,
                    'default_allowances_married': sp.default_allowances_married,
                    'default_allowances_hoh': sp.default_allowances_hoh,
                    'has_local_taxes': sp.has_local_taxes,
                    'local_tax_note': sp.local_tax_note,
                    'notes': f'Cloned from {source_year} — review for {target_year}',
                }
            )
            if created:
                for b in sp.brackets.all():
                    StateBracket.objects.create(
                        profile=new_sp,
                        filing_status=b.filing_status,
                        min_amount=b.min_amount,
                        max_amount=b.max_amount,
                        rate=b.rate,
                    )
                for i in sp.insurance_rates.all():
                    StateInsuranceRate.objects.create(
                        profile=new_sp,
                        category=i.category,
                        name=i.name,
                        rate=i.rate,
                        wage_base=i.wage_base,
                    )
                states_copied += 1

        self.stdout.write(f'Cloned {states_copied} state profiles from {source_year} to {target_year}')
        self.stdout.write(f'Total {target_year} states: {StateTaxProfile.objects.filter(year=target_year).count()}')

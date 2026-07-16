"""Server-side paycheck calculation API endpoint.

Accepts the same input format as the frontend calculator and returns
the full tax breakdown. Requires X-API-Key header for authentication.
"""
from decimal import Decimal

from django.db.models import Q
from rest_framework.decorators import api_view, authentication_classes, permission_classes, parser_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from saasclaw_engine.public_api.models import ApiKey
from .models import (
    FederalTaxYear, FederalBracket, StateTaxProfile, StateBracket,
    StateInsuranceRate, LocalTaxInfo, LocalTaxBracket,
)
from .models_pa import PaTaxCode


def _authenticate_api_key(request):
    """Validate X-API-Key header. Returns (apikey, user) or (None, None)."""
    key = request.META.get('HTTP_X_API_KEY', '').strip()
    if not key:
        return None, None
    return ApiKey.verify_key(key)


def _bracket_tax(income, brackets):
    """Calculate tax using progressive brackets."""
    tax = Decimal('0')
    remaining = income
    prev_min = Decimal('0')
    for bracket in brackets:
        bracket_min = bracket.min_amount
        bracket_max = bracket.max_amount  # None means no upper limit
        if income <= bracket_min:
            break
        taxable_in_bracket = min(income, bracket_max or Decimal('999999999')) - bracket_min
        if taxable_in_bracket > 0:
            tax += taxable_in_bracket * bracket.rate
    return tax


def _annualize(gross_pay, pay_frequency):
    """Convert pay-period amount to annual."""
    multipliers = {
        'annual': Decimal('1'),
        'monthly': Decimal('12'),
        'biweekly': Decimal('26'),
        'weekly': Decimal('52'),
        'hourly': Decimal('2080'),  # assume 40 hrs/week × 52 weeks
    }
    return gross_pay * multipliers.get(pay_frequency, Decimal('1'))


def _deannualize(annual_amount, pay_frequency):
    """Convert annual amount back to per-pay-period."""
    divisors = {
        'annual': Decimal('1'),
        'monthly': Decimal('12'),
        'biweekly': Decimal('26'),
        'weekly': Decimal('52'),
        'hourly': Decimal('2080'),
    }
    return annual_amount / divisors.get(pay_frequency, Decimal('1'))


def _get_filing_status_key(filing_status):
    """Map frontend filing status to model bracket filing_status values."""
    mapping = {
        'single': 'single',
        'married_filing_jointly': 'married',
        'married_filing_separately': 'single',  # usually same as single brackets
        'head_of_household': 'hoh',
    }
    return mapping.get(filing_status, 'single')


def _get_standard_deduction(federal, filing_status):
    """Get standard deduction for the filing status."""
    mapping = {
        'single': federal.standard_deduction_single,
        'married_filing_jointly': federal.standard_deduction_married,
        'married_filing_separately': federal.standard_deduction_married,
        'head_of_household': federal.standard_deduction_hoh,
    }
    return mapping.get(filing_status, federal.standard_deduction_single)


def _get_state_deduction(state_profile, filing_status):
    """Get state standard deduction for the filing status."""
    mapping = {
        'single': state_profile.standard_deduction_single,
        'married_filing_jointly': state_profile.standard_deduction_married,
        'married_filling_separately': state_profile.standard_deduction_married,
        'head_of_household': state_profile.standard_deduction_hoh,
    }
    return mapping.get(filing_status, state_profile.standard_deduction_single)


def _get_state_exemption(state_profile, filing_status, dependents=0):
    """Get state personal + dependent exemption."""
    personal = {
        'single': state_profile.personal_exemption_single,
        'married_filing_jointly': state_profile.personal_exemption_married,
        'married_filing_separately': state_profile.personal_exemption_married,
        'head_of_household': state_profile.personal_exemption_hoh,
    }.get(filing_status, state_profile.personal_exemption_single)
    return personal + (state_profile.dependent_exemption * dependents)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([JSONParser])
def calculate_view(request):
    """Calculate federal, state, and local tax withholding.

    Request body:
    {
        "grossPay": 75000,
        "payFrequency": "annual",  // annual|monthly|biweekly|weekly|hourly
        "state": "CA",             // 2-letter state code
        "filingStatus": "single",  // single|married_filing_jointly|married_filing_separately|head_of_household
        "taxYear": 2026,
        "dependentCredits": 0,
        "dependents": 0,
        "otherIncome": 0,
        "w4OtherDeductions": 0,
        "extraWithholding": 0,
        "section125": 0,
        "retirement401k": 0,
        "hsa": 0,
        "roth401k": 0,
        "otherDeductions": 0,
        "locality": "",
        "paResidentPsd": "",
        "paWorkPsd": ""
    }

    Requires X-API-Key header.
    """
    # --- Auth ---
    apikey, user = _authenticate_api_key(request)
    if not apikey:
        return Response({'error': 'Invalid or missing API key. Include X-API-Key header.'}, status=401)

    # Check usage limit
    if apikey.usage_limit and apikey.usage_count >= apikey.usage_limit:
        return Response({'error': 'API key usage limit exceeded.'}, status=429)

    # Increment usage
    apikey.usage_count += 1
    apikey.save(update_fields=['usage_count'])

    # --- Parse input ---
    data = request.data
    try:
        gross_pay = Decimal(str(data['grossPay']))
        pay_frequency = data.get('payFrequency', 'annual')
        state_code = data.get('state', '').upper()
        filing_status = data.get('filingStatus', 'single')
        tax_year = int(data.get('taxYear', 2026))
        dependent_credits = Decimal(str(data.get('dependentCredits', 0)))
        dependents = int(data.get('dependents', 0))
        other_income = Decimal(str(data.get('otherIncome', 0)))
        w4_other_deductions = Decimal(str(data.get('w4OtherDeductions', 0)))
        extra_withholding = Decimal(str(data.get('extraWithholding', 0)))
        section125 = Decimal(str(data.get('section125', 0)))
        retirement_401k = Decimal(str(data.get('retirement401k', 0)))
        hsa = Decimal(str(data.get('hsa', 0)))
        roth_401k = Decimal(str(data.get('roth401k', 0)))
        other_deductions = Decimal(str(data.get('otherDeductions', 0)))
        locality = data.get('locality', '')
        pa_resident_psd = data.get('paResidentPsd', '')
        pa_work_psd = data.get('paWorkPsd', '')
    except (KeyError, ValueError, TypeError) as e:
        return Response({'error': f'Invalid input: {e}'}, status=400)

    annual_gross = _annualize(gross_pay, pay_frequency)

    # --- Federal tax ---
    try:
        federal = FederalTaxYear.objects.get(year=tax_year, is_active=True)
    except FederalTaxYear.DoesNotExist:
        return Response({'error': f'Federal tax data not available for year {tax_year}'}, status=404)

    # Federal brackets
    fs_key = _get_filing_status_key(filing_status)
    bracket_schedule_map = {
        'single': 'income_single',
        'married': 'income_married',
        'hoh': 'income_hoh',
    }
    bracket_schedule = bracket_schedule_map.get(fs_key, 'income_single')

    federal_brackets = list(
        FederalBracket.objects.filter(tax_year=federal, schedule=bracket_schedule)
        .order_by('min_amount')
    )
    withholding_brackets = list(
        FederalBracket.objects.filter(
            tax_year=federal,
            schedule__startswith='withholding_standard'
        ).order_by('schedule', 'min_amount')
    )

    # Federal taxable income
    std_deduction = _get_standard_deduction(federal, filing_status)
    federal_taxable_income = max(Decimal('0'), annual_gross - std_deduction - (dependent_credits * Decimal('2000')))

    federal_income_tax = _bracket_tax(federal_taxable_income, federal_brackets)

    # FICA
    ss_wages = min(annual_gross, federal.social_security_wage_base)
    social_security_tax = ss_wages * federal.social_security_rate
    medicare_tax = annual_gross * federal.medicare_rate

    # Additional Medicare
    addl_medicare_threshold = {
        'single': federal.additional_medicare_threshold_single,
        'married_filing_jointly': federal.additional_medicare_threshold_mfj,
        'married_filing_separately': federal.additional_medicare_threshold_mfs,
        'head_of_household': federal.additional_medicare_threshold_hoh,
    }.get(filing_status, federal.additional_medicare_threshold_single)
    additional_medicare_tax = max(Decimal('0'), annual_gross - addl_medicare_threshold) * federal.additional_medicare_rate

    # --- State tax ---
    state_income_tax = Decimal('0')
    state_deduction = Decimal('0')
    state_dependent_exemption = Decimal('0')
    state_insurance_total = Decimal('0')
    state_insurance_items = []
    state_name = ''
    state_tax_type = 'none'

    if state_code:
        try:
            state_profile = StateTaxProfile.objects.get(year=tax_year, state_code=state_code)
            state_name = state_profile.state_name
            state_tax_type = state_profile.tax_type

            if state_profile.tax_type == 'flat':
                # Deductions
                if state_profile.withholding_method == 'allowance':
                    allow_key = {
                        'single': state_profile.default_allowances_single,
                        'married_filing_jointly': state_profile.default_allowances_married,
                        'married_filing_separately': state_profile.default_allowances_single,
                        'head_of_household': state_profile.default_allowances_hoh,
                    }.get(filing_status, state_profile.default_allowances_single)
                    allow_amount = {
                        'single': state_profile.withholding_allowance_single,
                        'married_filing_jointly': state_profile.withholding_allowance_married,
                        'married_filing_separately': state_profile.withholding_allowance_single,
                        'head_of_household': state_profile.withholding_allowance_hoh,
                    }.get(filing_status, state_profile.withholding_allowance_single)

                    if state_profile.allowance_includes_standard_deduction:
                        # MN-style: allowance replaces standard deduction
                        state_deduction = allow_key * allow_amount
                    else:
                        # GA-style: both standard deduction and allowances
                        state_deduction = _get_state_deduction(state_profile, filing_status)
                        state_deduction += allow_key * allow_amount
                else:
                    state_deduction = _get_state_deduction(state_profile, filing_status)
                    state_dependent_exemption = _get_state_exemption(state_profile, filing_status, dependents)
                    state_deduction += state_dependent_exemption

                state_taxable_income = max(Decimal('0'), annual_gross - state_deduction)
                state_income_tax = state_taxable_income * state_profile.flat_rate

            elif state_profile.tax_type == 'progressive':
                state_brackets = list(
                    StateBracket.objects.filter(
                        profile=state_profile,
                        filing_status=fs_key
                    ).order_by('min_amount')
                )
                if state_profile.withholding_method == 'allowance':
                    allow_key = {
                        'single': state_profile.default_allowances_single,
                        'married_filing_jointly': state_profile.default_allowances_married,
                        'married_filing_separately': state_profile.default_allowances_single,
                        'head_of_household': state_profile.default_allowances_hoh,
                    }.get(filing_status, state_profile.default_allowances_single)
                    allow_amount = {
                        'single': state_profile.withholding_allowance_single,
                        'married_filing_jointly': state_profile.withholding_allowance_married,
                        'married_filing_separately': state_profile.withholding_allowance_single,
                        'head_of_household': state_profile.withholding_allowance_hoh,
                    }.get(filing_status, state_profile.withholding_allowance_single)

                    if state_profile.allowance_includes_standard_deduction:
                        state_deduction = allow_key * allow_amount
                    else:
                        state_deduction = _get_state_deduction(state_profile, filing_status)
                        state_deduction += allow_key * allow_amount
                else:
                    state_deduction = _get_state_deduction(state_profile, filing_status)
                    state_dependent_exemption = _get_state_exemption(state_profile, filing_status, dependents)
                    state_deduction += state_dependent_exemption

                state_taxable_income = max(Decimal('0'), annual_gross - state_deduction)
                state_income_tax = _bracket_tax(state_taxable_income, state_brackets)

            # State insurance
            insurance_rates = StateInsuranceRate.objects.filter(profile=state_profile)
            for ins in insurance_rates:
                ins_wages = min(annual_gross, ins.wage_base) if ins.wage_base else annual_gross
                ins_amount = ins_wages * ins.rate
                state_insurance_total += ins_amount
                state_insurance_items.append({
                    'name': ins.name,
                    'amount': float(ins_amount.quantize(Decimal('0.01'))),
                })

        except StateTaxProfile.DoesNotExist:
            pass  # No state data — return zeros

    # --- Local tax ---
    local_income_tax = Decimal('0')
    local_tax_name = ''
    local_tax_description = ''
    pa_eit_tax = Decimal('0')
    pa_eit_description = ''
    pa_lst_tax = Decimal('0')
    pa_lst_description = ''

    if state_code and locality:
        try:
            local_tax_info = LocalTaxInfo.objects.get(
                profile__year=tax_year,
                profile__state_code=state_code,
                locality_code=locality
            )
            local_tax_name = local_tax_info.locality

            if local_tax_info.tax_type == 'flat' and local_tax_info.flat_rate:
                local_income_tax = annual_gross * local_tax_info.flat_rate
            elif local_tax_info.tax_type == 'progressive':
                local_brackets = list(
                    LocalTaxBracket.objects.filter(
                        local_tax=local_tax_info,
                        filing_status=fs_key
                    ).order_by('min_amount')
                )
                local_income_tax = _bracket_tax(annual_gross, local_brackets)
            elif local_tax_info.tax_type == 'surcharge' and local_tax_info.flat_rate:
                local_income_tax = state_income_tax * local_tax_info.flat_rate

            local_tax_description = local_tax_info.description
        except LocalTaxInfo.DoesNotExist:
            pass

    # --- PA local taxes (EIT/LST) ---
    if state_code == 'PA' and (pa_resident_psd or pa_work_psd):
        try:
            # Resident EIT
            if pa_resident_psd:
                resident_psd = PaTaxCode.objects.get(psd_code=pa_resident_psd, year=tax_year)
                pa_eit_tax = annual_gross * resident_psd.total_eit
                pa_eit_description = f'Resident EIT ({resident_psd.municipality})'
        except (PaTaxCode.DoesNotExist, Exception):
            pass

        try:
            # LST (work PSD)
            if pa_work_psd:
                work_psd = PaTaxCode.objects.get(psd_code=pa_work_psd, year=tax_year)
                pa_lst_tax = min(annual_gross, Decimal('12000')) * work_psd.lst_total if work_psd.lst_total else Decimal('0')
                pa_lst_description = f'LST ({work_psd.municipality})'
        except (PaTaxCode.DoesNotExist, Exception):
            pass

    # --- Totals ---
    total_tax = (
        federal_income_tax + state_income_tax + local_income_tax +
        social_security_tax + medicare_tax + additional_medicare_tax +
        state_insurance_total + pa_eit_tax + pa_lst_tax
    )
    net_pay = annual_gross - total_tax
    effective_rate = float(total_tax / annual_gross) if annual_gross > 0 else 0

    # Find marginal rate (federal bracket)
    marginal_rate = Decimal('0')
    for bracket in federal_brackets:
        if federal_taxable_income > bracket.min_amount:
            marginal_rate = bracket.rate

    # Per-paycheck amounts
    per = {}
    for key, value in [
        ('grossPay', annual_gross),
        ('federalIncomeTax', federal_income_tax),
        ('stateIncomeTax', state_income_tax),
        ('localIncomeTax', local_income_tax),
        ('stateInsuranceTotal', state_insurance_total),
        ('socialSecurityTax', social_security_tax),
        ('medicareTax', medicare_tax),
        ('additionalMedicareTax', additional_medicare_tax),
        ('extraWithholding', extra_withholding),
        ('totalTax', total_tax),
        ('netPay', net_pay),
        ('section125', section125),
        ('retirement401k', retirement_401k),
        ('hsa', hsa),
        ('roth401k', roth_401k),
        ('otherDeductions', other_deductions),
    ]:
        per[key] = float(_deannualize(value, pay_frequency).quantize(Decimal('0.01')))

    result = {
        'taxYear': tax_year,
        'input': {
            'grossPay': float(gross_pay),
            'payFrequency': pay_frequency,
            'state': state_code,
            'filingStatus': filing_status,
            'dependentCredits': float(dependent_credits),
            'dependents': dependents,
            'otherIncome': float(other_income),
            'section125': float(section125),
            'retirement401k': float(retirement_401k),
            'hsa': float(hsa),
            'roth401k': float(roth_401k),
        },
        'result': {
            'grossPay': float(annual_gross.quantize(Decimal('0.01'))),
            'federalIncomeTax': float(federal_income_tax.quantize(Decimal('0.01'))),
            'stateIncomeTax': float(state_income_tax.quantize(Decimal('0.01'))),
            'localIncomeTax': float(local_income_tax.quantize(Decimal('0.01'))),
            'socialSecurityTax': float(social_security_tax.quantize(Decimal('0.01'))),
            'medicareTax': float(medicare_tax.quantize(Decimal('0.01'))),
            'additionalMedicareTax': float(additional_medicare_tax.quantize(Decimal('0.01'))),
            'extraWithholding': float(extra_withholding.quantize(Decimal('0.01'))),
            'stateInsuranceTotal': float(state_insurance_total.quantize(Decimal('0.01'))),
            'stateInsuranceItems': state_insurance_items,
            'totalTax': float(total_tax.quantize(Decimal('0.01'))),
            'netPay': float(net_pay.quantize(Decimal('0.01'))),
            'effectiveTaxRate': round(effective_rate, 4),
            'marginalTaxRate': float(marginal_rate),
            'localTaxName': local_tax_name,
            'localTaxDescription': local_tax_description,
            'paEitTax': float(pa_eit_tax.quantize(Decimal('0.01'))),
            'paEitDescription': pa_eit_description,
            'paEitResidentPsd': pa_resident_psd,
            'paEitWorkPsd': pa_work_psd,
            'paLstTax': float(pa_lst_tax.quantize(Decimal('0.01'))),
            'paLstDescription': pa_lst_description,
            'perPaycheck': per,
            'federalTaxableIncome': float(federal_taxable_income.quantize(Decimal('0.01'))),
            'stateTaxableIncome': float(max(Decimal('0'), annual_gross - state_deduction).quantize(Decimal('0.01'))),
            'standardDeduction': float(std_deduction),
            'stateDeduction': float(state_deduction),
            'stateDependentExemption': float(state_dependent_exemption),
            'dependentCredits': float(dependent_credits),
            'ficaWages': float(ss_wages.quantize(Decimal('0.01'))),
            'federalIncomeWages': float(annual_gross.quantize(Decimal('0.01'))),
            'section125': float(section125),
            'retirement401k': float(retirement_401k),
            'hsa': float(hsa),
            'roth401k': float(roth_401k),
            'otherDeductions': float(other_deductions),
        },
    }

    return Response(result)
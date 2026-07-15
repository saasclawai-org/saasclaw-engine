"""Tax data models — federal and state tax profiles, brackets, and insurance rates.

Designed to be editable via admin API and served to the paycheck calculator frontend.
"""
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class FederalTaxYear(models.Model):
    """Federal tax data for a single tax year (brackets, FICA, standard deduction)."""
    year = models.PositiveIntegerField(unique=True, validators=[MinValueValidator(2020), MaxValueValidator(2035)])
    is_active = models.BooleanField(default=True, help_text='Only one active year at a time for the calculator')

    # FICA
    social_security_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0.062,
        help_text='Social Security tax rate (e.g. 0.062 = 6.2%)')
    social_security_wage_base = models.PositiveIntegerField(default=177000,
        help_text='Social Security wage base cap')
    medicare_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0.0145,
        help_text='Medicare tax rate (e.g. 0.0145 = 1.45%)')
    additional_medicare_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0.009,
        help_text='Additional Medicare rate above threshold')
    additional_medicare_threshold_single = models.PositiveIntegerField(default=200000)
    additional_medicare_threshold_mfj = models.PositiveIntegerField(default=250000)
    additional_medicare_threshold_mfs = models.PositiveIntegerField(default=125000)
    additional_medicare_threshold_hoh = models.PositiveIntegerField(default=200000)

    # Standard deduction
    standard_deduction_single = models.PositiveIntegerField(default=15000)
    standard_deduction_married = models.PositiveIntegerField(default=30000)
    standard_deduction_hoh = models.PositiveIntegerField(default=22500)

    # Pub 15-T deduction equivalent
    pub15t_deduction_single = models.PositiveIntegerField(default=8600,
        help_text='Line 1g deduction equivalent for standard schedule (single/MFS)')
    pub15t_deduction_married = models.PositiveIntegerField(default=12900,
        help_text='Line 1g deduction equivalent for standard schedule (MFJ)')

    note = models.TextField(blank=True, help_text='Optional notes about this tax year')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-year']
        verbose_name = 'Federal Tax Year'
        verbose_name_plural = 'Federal Tax Years'

    def __str__(self):
        return f'Federal Tax Year {self.year}'


class FederalBracket(models.Model):
    """A single tax bracket within a federal tax year."""
    tax_year = models.ForeignKey(FederalTaxYear, on_delete=models.CASCADE, related_name='brackets')
    schedule = models.CharField(max_length=40, choices=[
        ('income_single', 'Income Tax — Single'),
        ('income_married', 'Income Tax — MFJ'),
        ('income_hoh', 'Income Tax — HOH'),
        ('withholding_standard_single', 'Withholding Standard — Single/MFS'),
        ('withholding_standard_married', 'Withholding Standard — MFJ'),
        ('withholding_standard_hoh', 'Withholding Standard — HOH'),
        ('withholding_step2_single', 'Withholding Step 2 — Single/MFS'),
        ('withholding_step2_married', 'Withholding Step 2 — MFJ'),
        ('withholding_step2_hoh', 'Withholding Step 2 — HOH'),
    ])
    min_amount = models.PositiveIntegerField(help_text='Lower bound of bracket (annual)')
    max_amount = models.PositiveIntegerField(null=True, blank=True, help_text='Upper bound (null = no limit)')
    rate = models.DecimalField(max_digits=6, decimal_places=5, help_text='Tax rate (e.g. 0.22 = 22%)')

    class Meta:
        ordering = ['tax_year', 'schedule', 'min_amount']
        verbose_name = 'Federal Bracket'
        verbose_name_plural = 'Federal Brackets'

    def __str__(self):
        return f'{self.get_schedule_display()}: ${self.min_amount:,}–{self.max_amount or "∞"} @ {self.rate}'


class StateTaxProfile(models.Model):
    """State tax profile for a given tax year."""
    year = models.PositiveIntegerField(validators=[MinValueValidator(2020), MaxValueValidator(2035)])
    state_code = models.CharField(max_length=2, help_text='2-letter USPS state code (e.g. CA, NY)')
    state_name = models.CharField(max_length=50)

    TAX_TYPE_CHOICES = [
        ('none', 'No Income Tax'),
        ('flat', 'Flat Rate'),
        ('progressive', 'Progressive Brackets'),
    ]
    tax_type = models.CharField(max_length=12, choices=TAX_TYPE_CHOICES, default='progressive')

    flat_rate = models.DecimalField(max_digits=6, decimal_places=5, null=True, blank=True,
        help_text='Flat tax rate (e.g. 0.0495 = 4.95%). Only used when tax_type=flat.')

    # Standard deduction by filing status
    standard_deduction_single = models.PositiveIntegerField(default=0)
    standard_deduction_married = models.PositiveIntegerField(default=0)
    standard_deduction_hoh = models.PositiveIntegerField(default=0)

    # Personal exemption by filing status
    personal_exemption_single = models.PositiveIntegerField(default=0)
    personal_exemption_married = models.PositiveIntegerField(default=0)
    personal_exemption_hoh = models.PositiveIntegerField(default=0)

    # Dependent exemption
    dependent_exemption = models.PositiveIntegerField(default=0,
        help_text='Per-dependent exemption amount (e.g. $4,930 for SC)')

    # Withholding method
    WITHHOLDING_METHOD_CHOICES = [
        ('standard_deduction', 'Standard Deduction'),
        ('allowance', 'Allowance-Based'),
    ]
    withholding_method = models.CharField(max_length=20, choices=WITHHOLDING_METHOD_CHOICES, default='standard_deduction',
        help_text='How withholding is calculated: standard_deduction subtracts std ded + personal exemption from wages; '
                  'allowance subtracts (default_allowances × allowance_amount) from wages.')

    # Withholding allowance amount per allowance (for allowance-based states like MN)
    withholding_allowance_single = models.PositiveIntegerField(default=0,
        help_text='Per-allowance deduction amount for single filers (e.g. $4,950 for MN 2025)')
    withholding_allowance_married = models.PositiveIntegerField(default=0,
        help_text='Per-allowance deduction amount for married filers')
    withholding_allowance_hoh = models.PositiveIntegerField(default=0,
        help_text='Per-allowance deduction amount for head of household filers')

    # Default number of allowances for withholding (what a typical single/MFJ/HOH filer claims)
    default_allowances_single = models.PositiveIntegerField(default=0,
        help_text='Default allowances for single/MFS filers (e.g. 1 for MN)')
    default_allowances_married = models.PositiveIntegerField(default=0,
        help_text='Default allowances for married filing jointly')
    default_allowances_hoh = models.PositiveIntegerField(default=0,
        help_text='Default allowances for head of household')

    # Local taxes
    has_local_taxes = models.BooleanField(default=False)
    local_tax_note = models.TextField(blank=True)

    notes = models.TextField(blank=True, help_text='Internal notes about this state\'s tax rules')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('year', 'state_code')
        ordering = ['year', 'state_code']
        verbose_name = 'State Tax Profile'
        verbose_name_plural = 'State Tax Profiles'

    def __str__(self):
        return f'{self.state_code} ({self.year}) — {self.get_tax_type_display()}'


class StateBracket(models.Model):
    """A single tax bracket within a state tax profile."""
    profile = models.ForeignKey(StateTaxProfile, on_delete=models.CASCADE, related_name='brackets')
    filing_status = models.CharField(max_length=20, choices=[
        ('single', 'Single/MFS'),
        ('married', 'MFJ'),
        ('head_of_household', 'HOH'),
    ])
    min_amount = models.PositiveIntegerField(help_text='Lower bound of bracket (annual)')
    max_amount = models.PositiveIntegerField(null=True, blank=True, help_text='Upper bound (null = no limit)')
    rate = models.DecimalField(max_digits=6, decimal_places=5, help_text='Tax rate (e.g. 0.06 = 6%)')

    class Meta:
        ordering = ['profile', 'filing_status', 'min_amount']
        verbose_name = 'State Bracket'
        verbose_name_plural = 'State Brackets'

    def __str__(self):
        return f'{self.profile.state_code} {self.filing_status}: ${self.min_amount:,}–{self.max_amount or "∞"} @ {self.rate}'


class StateInsuranceRate(models.Model):
    """State disability/leave insurance rate (employee-paid)."""
    profile = models.ForeignKey(StateTaxProfile, on_delete=models.CASCADE, related_name='insurance_rates')
    category = models.CharField(max_length=10, choices=[
        ('sdi', 'SDI/TDI'),
        ('pfml', 'PFML'),
        ('sui', 'SUI'),
    ])
    name = models.CharField(max_length=50, help_text='Display name (e.g. "CA SDI + PFL")')
    rate = models.DecimalField(max_digits=8, decimal_places=6, help_text='Employee rate (e.g. 0.013 = 1.3%)')
    wage_base = models.PositiveIntegerField(null=True, blank=True,
        help_text='Annual wage cap (null = no cap)')

    class Meta:
        ordering = ['profile', 'category', 'name']
        verbose_name = 'State Insurance Rate'
        verbose_name_plural = 'State Insurance Rates'

    def __str__(self):
        return f'{self.name}: {self.rate}{" (cap: $" + str(self.wage_base) if self.wage_base else " (no cap)"}'
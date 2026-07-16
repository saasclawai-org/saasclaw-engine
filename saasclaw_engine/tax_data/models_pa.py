"""PA PSD Tax Code models — Pennsylvania local EIT/LST rates by PSD code.

Stored in Postgres alongside federal/state tax data, served via /api/v1/tax/pa/ endpoints.
"""

from django.db import models


class PaTaxCode(models.Model):
    """Pennsylvania PSD tax code — one row per municipality/school district per year.

    Covers Earned Income Tax (EIT) rates, Local Services Tax (LST) amounts,
    Low-Income Exemption (LIE) thresholds, and EIT collector contact info.
    """
    year = models.PositiveIntegerField(help_text='Tax year (e.g. 2026)')
    psd_code = models.CharField(max_length=8, help_text='PA PSD code (e.g. "010201")')
    tax_collection_district = models.CharField(max_length=100, blank=True, default='')
    county = models.CharField(max_length=50, blank=True, default='')
    municipality_id = models.CharField(max_length=20, blank=True, default='')
    municipality = models.CharField(max_length=100, blank=True, default='')
    school_district_id = models.CharField(max_length=20, blank=True, default='')
    school_district = models.CharField(max_length=100, blank=True, default='')

    # EIT rates (percentages stored as decimal, e.g. 1.0 = 1%)
    municipal_nonresident_eit_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0,
        help_text='Municipal nonresident EIT rate (%)')
    municipal_resident_eit_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0,
        help_text='Municipal resident EIT rate (%)')
    school_district_eit_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0,
        help_text='School district EIT rate (%)')
    school_district_pit_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0,
        help_text='School district PIT rate (%)')
    total_resident_eit_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0,
        help_text='Total resident EIT rate (%)')

    # LIE thresholds (dollars)
    municipal_eit_lie = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='Municipal EIT LIE threshold ($)')
    school_district_eit_lie = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='School district EIT LIE threshold ($)')

    # LST amounts (dollars/year)
    municipal_lst = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='Municipal LST amount ($/year)')
    school_district_lst = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='School district LST amount ($/year)')
    total_lst = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='Total LST amount ($/year)')

    # LST LIE thresholds (dollars)
    municipal_lst_lie = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='Municipal LST LIE threshold ($)')
    school_district_lst_lie = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text='School district LST LIE threshold ($)')

    # LST effective dates
    municipal_lst_effective_date = models.DateField(null=True, blank=True,
        help_text='Date municipal LST rates take effect')
    school_district_lst_effective_date = models.DateField(null=True, blank=True,
        help_text='Date school district LST rates take effect')

    # EIT Collector info
    eit_collector = models.CharField(max_length=200, blank=True, default='')
    eit_collector_address1 = models.CharField(max_length=200, blank=True, default='')
    eit_collector_city = models.CharField(max_length=100, blank=True, default='')
    eit_collector_state = models.CharField(max_length=2, blank=True, default='')
    eit_collector_zip = models.CharField(max_length=10, blank=True, default='')
    eit_collector_phone = models.CharField(max_length=50, blank=True, default='')
    eit_collector_email = models.CharField(max_length=200, blank=True, default='')
    eit_collector_website = models.CharField(max_length=300, blank=True, default='')

    date_last_updated = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('year', 'psd_code')
        ordering = ['year', 'psd_code']
        verbose_name = 'PA Tax Code'
        verbose_name_plural = 'PA Tax Codes'

    def __str__(self):
        return f'PA {self.psd_code} ({self.year}) — {self.municipality}'
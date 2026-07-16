"""PA Tax Code API views — public read + admin CRUD + bulk upsert + lookup."""
import logging
from datetime import datetime

from decimal import Decimal

logger = logging.getLogger(__name__)

from django.db import models
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response

from .models_pa import PaTaxCode
from .serializers_pa import (
    PaTaxCodeSerializer, PaTaxCodeListSerializer,
    PaTaxCodeLookupSerializer,
)


def _normalize_rate(val):
    """Convert percentage values to decimals. DCED stores 1.00 = 1%, 0.5 = 0.5%.
    Model stores as decimal: 1% = 0.01, 0.5% = 0.005.
    Always divide by 100 because all rate values are percentages."""
    if val is None or val == '' or str(val).strip() == '':
        return Decimal('0')
    try:
        num = Decimal(str(val).replace('%', '').strip())
    except Exception:
        return Decimal('0')
    return num / Decimal('100')


def _parse_date(val):
    """Parse date strings from DCED data into YYYY-MM-DD format for Django DateField.
    DCED dates come as M/D/YYYY (e.g. '1/12/2026' or '02/13/1969').
    Also handles YYYY-MM-DD and empty values."""
    if not val or str(val).strip() == '':
        return None
    val = str(val).strip()
    # Already in ISO format
    if '-' in val and len(val) == 10:
        return val
    # DCED M/D/YYYY format
    for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d'):
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    # Give up — return None rather than crash
    logger.warning(f'Could not parse date: {val!r}')
    return None


class LenientJWTAuthentication(JWTAuthentication):
    """JWT auth that silently ignores invalid/expired tokens.

    On public (AllowAny) endpoints, a stale client token should not
    cause a 401 — the request proceeds as anonymous instead.
    On admin (IsAdminUser) endpoints, the normal strict JWT check applies.
    """
    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except Exception:
            return None  # Invalid token → treat as anonymous


class PaTaxCodePagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class PaTaxCodeViewSet(viewsets.ModelViewSet):
    """PA PSD Tax Code CRUD + bulk upsert + lookup.

    Public: list, retrieve, years, lookup
    Admin: create, update, delete, bulk-upsert, bulk-upsert-and-replace
    """
    queryset = PaTaxCode.objects.all().order_by('year', 'psd_code')
    lookup_field = 'pk'
    pagination_class = PaTaxCodePagination
    authentication_classes = [LenientJWTAuthentication]

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'years', 'lookup'):
            return [AllowAny()]
        return [IsAdminUser()]



    def get_serializer_class(self):
        if self.action == 'list':
            return PaTaxCodeListSerializer
        if self.action == 'lookup':
            return PaTaxCodeLookupSerializer
        return PaTaxCodeSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        year = self.request.query_params.get('year')
        if year:
            qs = qs.filter(year=int(year))
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                models.Q(psd_code__icontains=search)
                | models.Q(municipality__icontains=search)
                | models.Q(school_district__icontains=search)
                | models.Q(county__icontains=search)
            )
        county = self.request.query_params.get('county')
        if county:
            qs = qs.filter(county__iexact=county)
        return qs

    @action(detail=False, methods=['get'], url_path='years')
    def years(self, request):
        """Public: list available years."""
        years = list(
            PaTaxCode.objects.values_list('year', flat=True)
            .distinct().order_by('-year')
        )
        return Response({'years': years})

    @action(detail=False, methods=['get'], url_path='lookup')
    def lookup(self, request):
        """Public: look up PA tax rates by PSD codes.

        Query params: year (required), psd_code (repeatable).
        Returns lightweight data for calculator use.
        """
        year = request.query_params.get('year')
        if not year:
            return Response({'error': 'year parameter required'}, status=400)
        psd_codes = request.query_params.getlist('psd_code')
        qs = PaTaxCode.objects.filter(year=int(year))
        if psd_codes:
            qs = qs.filter(psd_code__in=psd_codes)
        serializer = PaTaxCodeLookupSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='bulk-upsert')
    def bulk_upsert(self, request):
        """Admin: bulk create or update PA tax codes for a year.

        Payload: { "year": 2026, "records": [...] }
        Each record is a flat object matching PaTaxCode fields (minus id/timestamps).
        Returns: { "total": N, "created": M, "updated": K }
        """
        year = request.data.get('year')
        records = request.data.get('records', [])
        if not year or not isinstance(records, list):
            return Response({'error': 'year and records array required'}, status=400)

        created = 0
        updated = 0
        sample_rates = []
        for i, rec in enumerate(records):
            psd_code = rec.get('psd_code')
            if not psd_code:
                continue

            # Log first 3 records for debugging
            if i < 3:
                sample_rates.append({
                    'psd': str(psd_code),
                    'resident': rec.get('municipal_resident_eit_rate', 'MISSING'),
                    'total': rec.get('total_resident_eit_rate', 'MISSING'),
                })
            defaults = {
                'tax_collection_district': rec.get('tax_collection_district', ''),
                'county': rec.get('county', ''),
                'municipality_id': rec.get('municipality_id', ''),
                'municipality': rec.get('municipality', ''),
                'school_district_id': rec.get('school_district_id', ''),
                'school_district': rec.get('school_district', ''),
                'municipal_nonresident_eit_rate': _normalize_rate(rec.get('municipal_nonresident_eit_rate', 0)),
                'municipal_resident_eit_rate': _normalize_rate(rec.get('municipal_resident_eit_rate', 0)),
                'school_district_eit_rate': _normalize_rate(rec.get('school_district_eit_rate', 0)),
                'school_district_pit_rate': _normalize_rate(rec.get('school_district_pit_rate', 0)),
                'total_resident_eit_rate': _normalize_rate(rec.get('total_resident_eit_rate', 0)),
                'municipal_eit_lie': rec.get('municipal_eit_lie', 0),
                'school_district_eit_lie': rec.get('school_district_eit_lie', 0),
                'municipal_lst': rec.get('municipal_lst', 0),
                'school_district_lst': rec.get('school_district_lst', 0),
                'total_lst': rec.get('total_lst', 0),
                'municipal_lst_lie': rec.get('municipal_lst_lie', 0),
                'school_district_lst_lie': rec.get('school_district_lst_lie', 0),
                'municipal_lst_effective_date': _parse_date(rec.get('municipal_lst_effective_date')),
                'school_district_lst_effective_date': _parse_date(rec.get('school_district_lst_effective_date')),
                'eit_collector': rec.get('eit_collector', ''),
                'eit_collector_address1': rec.get('eit_collector_address1', ''),
                'eit_collector_city': rec.get('eit_collector_city', ''),
                'eit_collector_state': rec.get('eit_collector_state', ''),
                'eit_collector_zip': rec.get('eit_collector_zip', ''),
                'eit_collector_phone': rec.get('eit_collector_phone', ''),
                'eit_collector_email': rec.get('eit_collector_email', ''),
                'eit_collector_website': rec.get('eit_collector_website', ''),
                'date_last_updated': _parse_date(rec.get('date_last_updated')),
            }
            _, created_flag = PaTaxCode.objects.update_or_create(
                year=year, psd_code=psd_code,
                defaults=defaults,
            )
            if created_flag:
                created += 1
            else:
                updated += 1

        logger.info(f'PA bulk upsert: year={year}, total={len(records)}, created={created}, updated={updated}, sample_rates={sample_rates}')
        return Response({'total': created + updated, 'created': created, 'updated': updated, 'sample_rates': sample_rates})

    @action(detail=False, methods=['post'], url_path='bulk-upsert-and-replace')
    def bulk_upsert_and_replace(self, request):
        """Admin: delete all records for a year, then insert new ones.

        Payload: { "year": 2026, "records": [...] }
        Returns: { "total": N, "created": N, "updated": 0 }
        """
        year = request.data.get('year')
        records = request.data.get('records', [])
        if not year or not isinstance(records, list):
            return Response({'error': 'year and records array required'}, status=400)

        # Delete existing records for this year
        PaTaxCode.objects.filter(year=year).delete()

        created = 0
        for rec in records:
            psd_code = rec.get('psd_code')
            if not psd_code:
                continue
            PaTaxCode.objects.create(
                year=year,
                psd_code=psd_code,
                tax_collection_district=rec.get('tax_collection_district', ''),
                county=rec.get('county', ''),
                municipality_id=rec.get('municipality_id', ''),
                municipality=rec.get('municipality', ''),
                school_district_id=rec.get('school_district_id', ''),
                school_district=rec.get('school_district', ''),
                municipal_nonresident_eit_rate=_normalize_rate(rec.get('municipal_nonresident_eit_rate', 0)),
                municipal_resident_eit_rate=_normalize_rate(rec.get('municipal_resident_eit_rate', 0)),
                school_district_eit_rate=_normalize_rate(rec.get('school_district_eit_rate', 0)),
                school_district_pit_rate=_normalize_rate(rec.get('school_district_pit_rate', 0)),
                total_resident_eit_rate=_normalize_rate(rec.get('total_resident_eit_rate', 0)),
                municipal_eit_lie=rec.get('municipal_eit_lie', 0),
                school_district_eit_lie=rec.get('school_district_eit_lie', 0),
                municipal_lst=rec.get('municipal_lst', 0),
                school_district_lst=rec.get('school_district_lst', 0),
                total_lst=rec.get('total_lst', 0),
                municipal_lst_lie=rec.get('municipal_lst_lie', 0),
                school_district_lst_lie=rec.get('school_district_lst_lie', 0),
                municipal_lst_effective_date=_parse_date(rec.get('municipal_lst_effective_date')),
                school_district_lst_effective_date=_parse_date(rec.get('school_district_lst_effective_date')),
                eit_collector=rec.get('eit_collector', ''),
                eit_collector_address1=rec.get('eit_collector_address1', ''),
                eit_collector_city=rec.get('eit_collector_city', ''),
                eit_collector_state=rec.get('eit_collector_state', ''),
                eit_collector_zip=rec.get('eit_collector_zip', ''),
                eit_collector_phone=rec.get('eit_collector_phone', ''),
                eit_collector_email=rec.get('eit_collector_email', ''),
                eit_collector_website=rec.get('eit_collector_website', ''),
                date_last_updated=_parse_date(rec.get('date_last_updated')),
            )
            created += 1

        return Response({'total': created, 'created': created, 'updated': 0})

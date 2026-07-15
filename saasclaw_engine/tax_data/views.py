"""Tax data API views — public GET and admin CRUD."""
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response

from .models import FederalTaxYear, StateTaxProfile
from .serializers import (
    FederalTaxYearSerializer, FederalTaxYearWriteSerializer,
    StateTaxProfileSerializer, StateTaxProfileListSerializer, StateTaxProfileWriteSerializer,
)


class FederalTaxYearViewSet(viewsets.ModelViewSet):
    """Admin CRUD + public read for federal tax year data."""
    queryset = FederalTaxYear.objects.prefetch_related('brackets').all()
    lookup_field = 'year'

    def get_permissions(self):
        """Public read, admin write."""
        if self.action in ('list', 'retrieve', 'active'):
            return [AllowAny()]
        return [IsAdminUser()]

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return FederalTaxYearWriteSerializer
        return FederalTaxYearSerializer

    @action(detail=False, methods=['get'], url_path='active')
    def active(self, request):
        """Public: get the currently active federal tax year with all brackets."""
        tax_year = get_object_or_404(FederalTaxYear, is_active=True)
        serializer = FederalTaxYearSerializer(tax_year)
        return Response(serializer.data)


class StateTaxProfileViewSet(viewsets.ModelViewSet):
    """Admin CRUD + public read for state tax profiles."""
    queryset = StateTaxProfile.objects.prefetch_related('brackets', 'insurance_rates').all()
    lookup_field = 'pk'

    def get_permissions(self):
        """Public read, admin write."""
        if self.action in ('list', 'retrieve', 'by_year', 'sources'):
            return [AllowAny()]
        return [IsAdminUser()]

    def get_serializer_class(self):
        if self.action == 'list':
            return StateTaxProfileListSerializer
        if self.action in ('create', 'update', 'partial_update'):
            return StateTaxProfileWriteSerializer
        return StateTaxProfileSerializer

    @action(detail=False, methods=['get'], url_path='year/(?P<year>\\d{4})')
    def by_year(self, request, year=None):
        """Public: get all state tax profiles for a given year."""
        profiles = self.queryset.filter(year=int(year))
        serializer = StateTaxProfileSerializer(profiles, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='sources')
    def sources(self, request):
        """Public: get source references for all states with source_url set."""
        profiles = StateTaxProfile.objects.filter(source_url__gt='').values(
            'year', 'state_code', 'state_name', 'source_url', 'source_name', 'last_verified',
            'agency_name', 'agency_phone', 'agency_email'
        )
        return Response(list(profiles))
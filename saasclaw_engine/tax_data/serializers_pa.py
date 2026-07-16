"""PA Tax Code serializers — public read + admin CRUD."""
from rest_framework import serializers
from .models_pa import PaTaxCode


class PaTaxCodeSerializer(serializers.ModelSerializer):
    """Public serializer — full read of PA tax code data."""

    class Meta:
        model = PaTaxCode
        fields = [
            'id', 'year', 'psd_code', 'tax_collection_district', 'county',
            'municipality_id', 'municipality', 'school_district_id', 'school_district',
            'municipal_nonresident_eit_rate', 'municipal_resident_eit_rate',
            'school_district_eit_rate', 'school_district_pit_rate', 'total_resident_eit_rate',
            'municipal_eit_lie', 'school_district_eit_lie',
            'municipal_lst', 'school_district_lst', 'total_lst',
            'municipal_lst_lie', 'school_district_lst_lie',
            'municipal_lst_effective_date', 'school_district_lst_effective_date',
            'eit_collector', 'eit_collector_address1', 'eit_collector_city',
            'eit_collector_state', 'eit_collector_zip', 'eit_collector_phone',
            'eit_collector_email', 'eit_collector_website',
            'date_last_updated', 'created_at', 'updated_at',
        ]


class PaTaxCodeListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for paginated list views."""

    class Meta:
        model = PaTaxCode
        fields = [
            'id', 'year', 'psd_code', 'county', 'municipality', 'school_district',
            'municipal_nonresident_eit_rate', 'municipal_resident_eit_rate',
            'total_resident_eit_rate', 'total_lst',
        ]


class PaTaxCodeUpsertSerializer(serializers.Serializer):
    """Serializer for bulk-upsert payload — accepts year + records array."""
    year = serializers.IntegerField()
    records = serializers.ListField()

    def validate_records(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('records must be an array')
        return value


class PaTaxCodeLookupSerializer(serializers.ModelSerializer):
    """Lightweight serializer for calculator lookups."""

    class Meta:
        model = PaTaxCode
        fields = [
            'psd_code', 'municipality', 'school_district', 'county',
            'municipal_resident_eit_rate', 'municipal_nonresident_eit_rate',
            'school_district_eit_rate', 'school_district_pit_rate',
            'total_resident_eit_rate',
            'municipal_eit_lie', 'school_district_eit_lie',
            'municipal_lst', 'school_district_lst', 'total_lst',
            'municipal_lst_lie', 'school_district_lst_lie',
        ]
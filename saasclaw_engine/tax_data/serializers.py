"""Tax data API — public endpoints for calculator, admin endpoints for editing."""

from rest_framework import serializers
from .models import FederalTaxYear, FederalBracket, StateTaxProfile, StateBracket, StateInsuranceRate


class FederalBracketSerializer(serializers.ModelSerializer):
    class Meta:
        model = FederalBracket
        fields = ['schedule', 'min_amount', 'max_amount', 'rate']


class FederalTaxYearSerializer(serializers.ModelSerializer):
    brackets = FederalBracketSerializer(many=True, read_only=True)
    additional_medicare_threshold = serializers.SerializerMethodField()

    class Meta:
        model = FederalTaxYear
        fields = [
            'year', 'is_active',
            'social_security_rate', 'social_security_wage_base',
            'medicare_rate', 'additional_medicare_rate',
            'additional_medicare_threshold',
            'standard_deduction_single', 'standard_deduction_married', 'standard_deduction_hoh',
            'pub15t_deduction_single', 'pub15t_deduction_married',
            'note', 'brackets',
        ]

    def get_additional_medicare_threshold(self, obj):
        return {
            'single': obj.additional_medicare_threshold_single,
            'married_filing_jointly': obj.additional_medicare_threshold_mfj,
            'married_filing_separately': obj.additional_medicare_threshold_mfs,
            'head_of_household': obj.additional_medicare_threshold_hoh,
        }


class StateBracketSerializer(serializers.ModelSerializer):
    class Meta:
        model = StateBracket
        fields = ['filing_status', 'min_amount', 'max_amount', 'rate']


class StateInsuranceRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = StateInsuranceRate
        fields = ['category', 'name', 'rate', 'wage_base']


class StateTaxProfileSerializer(serializers.ModelSerializer):
    brackets = StateBracketSerializer(many=True, read_only=True)
    insurance_rates = StateInsuranceRateSerializer(many=True, read_only=True)

    class Meta:
        model = StateTaxProfile
        fields = [
            'id', 'year', 'state_code', 'state_name', 'tax_type', 'flat_rate',
            'standard_deduction_single', 'standard_deduction_married', 'standard_deduction_hoh',
            'personal_exemption_single', 'personal_exemption_married', 'personal_exemption_hoh',
            'dependent_exemption',
            'withholding_method',
            'withholding_allowance_single', 'withholding_allowance_married', 'withholding_allowance_hoh',
            'default_allowances_single', 'default_allowances_married', 'default_allowances_hoh',
            'has_local_taxes', 'local_tax_note', 'notes',
            'brackets', 'insurance_rates',
        ]


class StateTaxProfileListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views."""
    class Meta:
        model = StateTaxProfile
        fields = ['id', 'year', 'state_code', 'state_name', 'tax_type', 'flat_rate']


# ─── Write serializers (admin) ─────────────────────────────────────────

class StateBracketWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StateBracket
        fields = ['filing_status', 'min_amount', 'max_amount', 'rate']


class StateInsuranceRateWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StateInsuranceRate
        fields = ['category', 'name', 'rate', 'wage_base']


class StateTaxProfileWriteSerializer(serializers.ModelSerializer):
    brackets = StateBracketWriteSerializer(many=True, required=False)
    insurance_rates = StateInsuranceRateWriteSerializer(many=True, required=False)

    class Meta:
        model = StateTaxProfile
        fields = [
            'year', 'state_code', 'state_name', 'tax_type', 'flat_rate',
            'standard_deduction_single', 'standard_deduction_married', 'standard_deduction_hoh',
            'personal_exemption_single', 'personal_exemption_married', 'personal_exemption_hoh',
            'dependent_exemption',
            'withholding_method',
            'withholding_allowance_single', 'withholding_allowance_married', 'withholding_allowance_hoh',
            'default_allowances_single', 'default_allowances_married', 'default_allowances_hoh',
            'has_local_taxes', 'local_tax_note', 'notes',
            'brackets', 'insurance_rates',
        ]

    def create(self, validated_data):
        brackets_data = validated_data.pop('brackets', [])
        insurance_data = validated_data.pop('insurance_rates', [])
        profile = StateTaxProfile.objects.create(**validated_data)
        for bracket in brackets_data:
            StateBracket.objects.create(profile=profile, **bracket)
        for ins in insurance_data:
            StateInsuranceRate.objects.create(profile=profile, **ins)
        return profile

    def update(self, instance, validated_data):
        brackets_data = validated_data.pop('brackets', None)
        insurance_data = validated_data.pop('insurance_rates', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if brackets_data is not None:
            instance.brackets.all().delete()
            for bracket in brackets_data:
                StateBracket.objects.create(profile=instance, **bracket)

        if insurance_data is not None:
            instance.insurance_rates.all().delete()
            for ins in insurance_data:
                StateInsuranceRate.objects.create(profile=instance, **ins)

        return instance


class FederalBracketWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = FederalBracket
        fields = ['schedule', 'min_amount', 'max_amount', 'rate']


class FederalTaxYearWriteSerializer(serializers.ModelSerializer):
    brackets = FederalBracketWriteSerializer(many=True, required=False)

    class Meta:
        model = FederalTaxYear
        fields = [
            'year', 'is_active',
            'social_security_rate', 'social_security_wage_base',
            'medicare_rate', 'additional_medicare_rate',
            'additional_medicare_threshold_single', 'additional_medicare_threshold_mfj',
            'additional_medicare_threshold_mfs', 'additional_medicare_threshold_hoh',
            'standard_deduction_single', 'standard_deduction_married', 'standard_deduction_hoh',
            'pub15t_deduction_single', 'pub15t_deduction_married',
            'note', 'brackets',
        ]

    def create(self, validated_data):
        brackets_data = validated_data.pop('brackets', [])
        tax_year = FederalTaxYear.objects.create(**validated_data)
        for bracket in brackets_data:
            FederalBracket.objects.create(tax_year=tax_year, **bracket)
        return tax_year

    def update(self, instance, validated_data):
        brackets_data = validated_data.pop('brackets', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if brackets_data is not None:
            instance.brackets.all().delete()
            for bracket in brackets_data:
                FederalBracket.objects.create(tax_year=instance, **bracket)

        return instance
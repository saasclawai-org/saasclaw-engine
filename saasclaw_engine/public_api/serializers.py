"""DRF serializers for the public API."""
from rest_framework import serializers
from django.contrib.auth.models import User

from saasclaw_engine.projects.models import Project
from saasclaw_engine.deployments.models import EnvironmentVariable


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email']


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)
    name = serializers.CharField(required=False, max_length=200)

    def create(self, validated_data):
        email = validated_data['email']
        password = validated_data['password']
        name = validated_data.get('name', '')
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=name,
        )
        return user


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = [
            'id', 'name', 'slug', 'description', 'framework',
            'status', 'preview_domain', 'production_domain',
            'onboarding_step', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'preview_domain', 'production_domain',
                            'onboarding_step', 'created_at', 'updated_at']


class ProjectCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    framework = serializers.CharField(max_length=32)
    description = serializers.CharField(required=False, default='', allow_blank=True)


class EnvVarSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=200)
    value = serializers.CharField()
    is_secret = serializers.BooleanField(default=True)


class SessionCreateSerializer(serializers.Serializer):
    pass  # Empty for now, sessions auto-created


class DeployTriggerSerializer(serializers.Serializer):
    environment = serializers.CharField(default='preview')


class GitCommitSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=500)
"""Tests for the Form Submission API.

Covers: API key authentication, rate limiting, origin validation,
honeypot anti-spam, JSON and form-encoded submissions, and
management endpoints (list, detail, delete).
"""

import json
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings
from django.core.cache import cache

from saasclaw_engine.projects.models import Project, FormSubmission
from saasclaw_engine.integrations.forms_api import (
    submit_form,
    form_submissions,
    form_submission_detail,
    _check_rate_limit,
    _validate_origin,
    _can_manage,
    HONEYPOT_FIELD,
)

User = get_user_model()


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear rate limit cache between tests."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner():
    return User.objects.create_user(username='owner', password='pass')


@pytest.fixture
def staff():
    return User.objects.create_user(username='staff', password='pass', is_staff=True)


@pytest.fixture
def stranger():
    return User.objects.create_user(username='stranger', password='pass')


@pytest.fixture
def project(owner):
    p = Project.objects.create(
        owner=owner, name='Test Project', slug='test-project',
        status=Project.Status.ACTIVE,
        preview_domain='test-project.preview.saasclaw.ai',
        production_domain='test-project.saasclaw.ai',
    )
    p.form_api_key = 'test-api-key-12345'
    p.save(update_fields=['form_api_key'])
    return p


class TestSubmitForm:
    """Tests for POST /api/forms/{slug}/ endpoint."""

    @pytest.mark.django_db
    def test_json_submission_success(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data=json.dumps({'name': 'Alice', 'email': 'alice@test.com'}),
            content_type='application/json',
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 201
        data = json.loads(response.content)
        assert data['ok'] is True
        assert data['id'] > 0

    @pytest.mark.django_db
    def test_form_encoded_submission_success(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data={'name': 'Bob', '_form_key': 'test-api-key-12345'},
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon2', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 201

    @pytest.mark.django_db
    def test_no_api_key_returns_403(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data=json.dumps({'name': 'Hacker'}),
            content_type='application/json',
        )
        request.user = User.objects.create_user(username='anon3', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_wrong_api_key_returns_403(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data=json.dumps({'name': 'Hacker'}),
            content_type='application/json',
            HTTP_X_FORM_KEY='wrong-key',
        )
        request.user = User.objects.create_user(username='anon4', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_key_in_body_field(self, project):
        """API key can be passed as _form_key form field."""
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data={'name': 'Carol', '_form_key': 'test-api-key-12345'},
        )
        request.user = User.objects.create_user(username='anon5', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 201

    @pytest.mark.django_db
    def test_honeypot_silently_drops(self, project):
        """Filled honeypot field returns success (200) but doesn't store."""
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data={'name': 'Bot', HONEYPOT_FIELD: 'http://spam.com'},
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon6', password='pass')
        response = submit_form(request, project.slug)
        # Honeypot returns 200 (not 201) to not alert bots
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data['id'] == 0  # Fake ID = not stored
        assert FormSubmission.objects.filter(project=project).count() == 0

    @pytest.mark.django_db
    def test_honeypot_stripped_from_data(self, project):
        """Empty honeypot field is removed from stored data."""
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data={'name': 'Dave', 'email': 'dave@test.com', HONEYPOT_FIELD: ''},
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon7', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 201
        sub = FormSubmission.objects.filter(project=project).first()
        assert sub is not None
        assert HONEYPOT_FIELD not in sub.form_data
        assert '_form_key' not in sub.form_data

    @pytest.mark.django_db
    def test_no_data_returns_400(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data=json.dumps({}),
            content_type='application/json',
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon8', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_invalid_json_returns_400(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data='not-json',
            content_type='application/json',
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon9', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_project_not_found_returns_404(self):
        factory = RequestFactory()
        request = factory.post(
            '/api/forms/nonexistent/',
            data=json.dumps({'name': 'Test'}),
            content_type='application/json',
            HTTP_X_FORM_KEY='test-api-key-12345',
        )
        request.user = User.objects.create_user(username='anon10', password='pass')
        response = submit_form(request, 'nonexistent')
        assert response.status_code == 404

    @pytest.mark.django_db
    def test_submission_records_metadata(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            data=json.dumps({'name': 'Eve'}),
            content_type='application/json',
            HTTP_X_FORM_KEY='test-api-key-12345',
            HTTP_USER_AGENT='TestBrowser/1.0',
            HTTP_REFERER='https://test-project.saasclaw.ai/contact',
            REMOTE_ADDR='1.2.3.4',
        )
        request.user = User.objects.create_user(username='anon11', password='pass')
        response = submit_form(request, project.slug)
        assert response.status_code == 201
        sub = FormSubmission.objects.filter(project=project).first()
        assert sub.ip_address == '1.2.3.4'
        assert sub.user_agent == 'TestBrowser/1.0'
        assert sub.referrer == 'https://test-project.saasclaw.ai/contact'


class TestRateLimiting:
    """Tests for rate limiting on form submissions."""

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    @pytest.mark.django_db
    def test_allows_within_limit(self):
        assert _check_rate_limit('test-proj', '1.1.1.1') is True

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    @pytest.mark.django_db
    def test_blocks_over_limit(self):
        for _ in range(10):
            assert _check_rate_limit('test-proj', '2.2.2.2') is True
        assert _check_rate_limit('test-proj', '2.2.2.2') is False

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    @pytest.mark.django_db
    def test_separate_ip_not_limited(self):
        for _ in range(10):
            _check_rate_limit('test-proj', '3.3.3.3')
        assert _check_rate_limit('test-proj', '4.4.4.4') is True

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    @pytest.mark.django_db
    def test_separate_project_not_limited(self):
        for _ in range(10):
            _check_rate_limit('proj-a', '5.5.5.5')
        assert _check_rate_limit('proj-b', '5.5.5.5') is True


class TestOriginValidation:
    """Tests for origin/referer validation."""

    @pytest.mark.django_db
    def test_matching_origin_allowed(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            HTTP_ORIGIN='https://test-project.preview.saasclaw.ai',
        )
        assert _validate_origin(request, project) is True

    @pytest.mark.django_db
    def test_matching_production_origin_allowed(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            HTTP_ORIGIN='https://test-project.saasclaw.ai',
        )
        assert _validate_origin(request, project) is True

    @pytest.mark.django_db
    def test_matching_referer_allowed(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            HTTP_REFERER='https://test-project.preview.saasclaw.ai/page',
        )
        assert _validate_origin(request, project) is True

    @pytest.mark.django_db
    def test_mismatched_origin_rejected(self, project):
        factory = RequestFactory()
        request = factory.post(
            f'/api/forms/{project.slug}/',
            HTTP_ORIGIN='https://evil.com',
        )
        assert _validate_origin(request, project) is False

    @pytest.mark.django_db
    def test_no_origin_header_allowed(self, project):
        """Requests without Origin header (curl, API calls) are allowed."""
        factory = RequestFactory()
        request = factory.post(f'/api/forms/{project.slug}/')
        assert _validate_origin(request, project) is True


class TestFormManagementEndpoints:
    """Tests for listing and managing form submissions."""

    @pytest.mark.django_db
    def test_list_submissions_owner(self, project, owner):
        FormSubmission.objects.create(
            project=project, form_data={'name': 'Alice'}, ip_address='1.1.1.1'
        )
        FormSubmission.objects.create(
            project=project, form_data={'name': 'Bob'}, ip_address='2.2.2.2'
        )
        factory = RequestFactory()
        request = factory.get(f'/api/forms/{project.slug}/list/')
        request.user = owner
        response = form_submissions(request, project.slug)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data['ok'] is True
        assert data['total'] == 2

    @pytest.mark.django_db
    def test_list_submissions_staff(self, project, staff):
        factory = RequestFactory()
        request = factory.get(f'/api/forms/{project.slug}/list/')
        request.user = staff
        response = form_submissions(request, project.slug)
        assert response.status_code == 200

    @pytest.mark.django_db
    def test_list_submissions_stranger_denied(self, project, stranger):
        factory = RequestFactory()
        request = factory.get(f'/api/forms/{project.slug}/list/')
        request.user = stranger
        response = form_submissions(request, project.slug)
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_list_submissions_anonymous_denied(self, project):
        factory = RequestFactory()
        request = factory.get(f'/api/forms/{project.slug}/list/')
        request.user = User.objects.create_user(username='anon', password='pass')
        response = form_submissions(request, project.slug)
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_delete_single_submission(self, project, owner):
        sub = FormSubmission.objects.create(
            project=project, form_data={'name': 'Alice'}, ip_address='1.1.1.1'
        )
        factory = RequestFactory()
        request = factory.delete(f'/api/forms/{project.slug}/{sub.id}/')
        request.user = owner
        response = form_submission_detail(request, project.slug, sub.id)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data['deleted'] is True
        assert FormSubmission.objects.filter(id=sub.id).count() == 0

    @pytest.mark.django_db
    def test_delete_all_submissions(self, project, owner):
        FormSubmission.objects.create(project=project, form_data={'name': 'A'})
        FormSubmission.objects.create(project=project, form_data={'name': 'B'})
        factory = RequestFactory()
        request = factory.delete(f'/api/forms/{project.slug}/list/')
        request.user = owner
        response = form_submissions(request, project.slug)
        assert response.status_code == 200
        assert FormSubmission.objects.filter(project=project).count() == 0

    @pytest.mark.django_db
    def test_detail_submission(self, project, owner):
        sub = FormSubmission.objects.create(
            project=project, form_data={'name': 'Alice', 'email': 'a@b.com'},
            ip_address='1.1.1.1', user_agent='Chrome', referrer='https://example.com',
        )
        factory = RequestFactory()
        request = factory.get(f'/api/forms/{project.slug}/{sub.id}/')
        request.user = owner
        response = form_submission_detail(request, project.slug, sub.id)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data['form_data']['name'] == 'Alice'
        assert data['ip_address'] == '1.1.1.1'
        assert data['user_agent'] == 'Chrome'


class TestProjectFormApiKey:
    """Tests for the form_api_key field on Project model."""

    @pytest.mark.django_db
    def test_blank_by_default(self, owner):
        project = Project.objects.create(
            owner=owner, name='No Key', slug='no-key'
        )
        assert project.form_api_key == ''

    @pytest.mark.django_db
    def test_get_or_create_generates_key(self, owner):
        project = Project.objects.create(
            owner=owner, name='Key Proj', slug='key-proj'
        )
        key = project.get_or_create_form_api_key()
        assert len(key) > 0
        project.refresh_from_db()
        assert project.form_api_key == key

    @pytest.mark.django_db
    def test_get_or_create_returns_existing(self, owner):
        project = Project.objects.create(
            owner=owner, name='Key Proj2', slug='key-proj2', form_api_key='existing-key'
        )
        key = project.get_or_create_form_api_key()
        assert key == 'existing-key'

    @pytest.mark.django_db
    def test_key_is_persistent(self, owner):
        project = Project.objects.create(
            owner=owner, name='Key Proj3', slug='key-proj3'
        )
        key1 = project.get_or_create_form_api_key()
        project.refresh_from_db()
        key2 = project.get_or_create_form_api_key()
        assert key1 == key2

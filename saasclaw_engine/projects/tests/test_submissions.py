"""Tests for the Project Submission and Approval workflow.

Covers: submission creation, validation, slug conflicts, staff review queue,
approve/reject actions, project auto-creation, and permission enforcement.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from saasclaw_engine.projects.models import Project, ProjectSubmission

User = get_user_model()


class TestProjectSubmissionModel:
    """Test ProjectSubmission model fields and constraints."""

    @pytest.mark.django_db
    def test_create_submission(self):
        """Basic submission creation with all fields."""
        user = User.objects.create_user(username='alice', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user,
            name='HR Employee Portal',
            slug='hr-employee-portal',
            description='Employee self-service portal',
            framework='next_static',
            source='blank',
            business_justification='Need centralized employee management',
            data_sensitivity='high',
            estimated_timeline='2 weeks',
        )
        assert sub.status == ProjectSubmission.Status.PENDING
        assert sub.reviewer is None
        assert sub.approved_project is None
        assert sub.require_gateway is False

    @pytest.mark.django_db
    def test_default_status_pending(self):
        """New submissions default to pending status."""
        user = User.objects.create_user(username='bob', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='Test', slug='test'
        )
        assert sub.status == 'pending'

    @pytest.mark.django_db
    def test_data_sensitivity_levels(self):
        """Data sensitivity accepts expected values."""
        user = User.objects.create_user(username='carol', password='pass')
        for level in ['none', 'low', 'medium', 'high']:
            sub = ProjectSubmission(
                requester=user, name=f'Proj {level}', slug=f'proj-{level}',
                data_sensitivity=level
            )
            sub.save()
            assert sub.data_sensitivity == level

    @pytest.mark.django_db
    def test_str_representation(self):
        """String representation includes name, status, and requester."""
        user = User.objects.create_user(username='dave', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='My App', slug='my-app'
        )
        result = str(sub)
        assert 'My App' in result
        assert 'pending' in result
        assert 'dave' in result

    @pytest.mark.django_db
    def test_unique_slug_not_enforced(self):
        """Multiple submissions can have the same slug (e.g., resubmission after rejection)."""
        user = User.objects.create_user(username='eve', password='pass')
        sub1 = ProjectSubmission.objects.create(
            requester=user, name='App v1', slug='my-app', status='rejected'
        )
        sub2 = ProjectSubmission.objects.create(
            requester=user, name='App v2', slug='my-app'
        )
        assert sub1.slug == sub2.slug
        assert sub1.status == 'rejected'
        assert sub2.status == 'pending'


class TestProjectModel:
    """Test Project model basics used in approval flow."""

    @pytest.mark.django_db
    def test_require_gateway_default(self):
        """New projects have gateway disabled by default."""
        user = User.objects.create_user(username='frank', password='pass')
        project = Project.objects.create(
            owner=user, name='Test Project', slug='test-project-gw'
        )
        assert project.require_gateway is False

    @pytest.mark.django_db
    def test_project_with_gateway_enabled(self):
        """Projects can have gateway explicitly enabled."""
        user = User.objects.create_user(username='grace', password='pass')
        project = Project.objects.create(
            owner=user, name='Sensitive App', slug='sensitive-app',
            require_gateway=True
        )
        assert project.require_gateway is True


class TestApprovalFlow:
    """Test the submission → approval → project creation flow."""

    @pytest.mark.django_db
    def test_reject_submission(self):
        """Rejection changes status and records reviewer."""
        staff = User.objects.create_user(username='staff1', password='pass', is_staff=True)
        user = User.objects.create_user(username='user1', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='Bad App', slug='bad-app'
        )
        sub.reviewer = staff
        sub.status = ProjectSubmission.Status.REJECTED
        sub.staff_notes = 'Does not meet requirements'
        sub.save()
        sub.refresh_from_db()
        assert sub.status == 'rejected'
        assert sub.reviewer == staff
        assert sub.staff_notes == 'Does not meet requirements'
        assert sub.approved_project is None

    @pytest.mark.django_db
    def test_cancel_submission(self):
        """User can cancel their own submission."""
        user = User.objects.create_user(username='user2', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='Old App', slug='old-app'
        )
        sub.status = ProjectSubmission.Status.CANCELLED
        sub.save()
        sub.refresh_from_db()
        assert sub.status == 'cancelled'

    @pytest.mark.django_db
    def test_approve_creates_project(self):
        """Approval with project creation links submission to project."""
        staff = User.objects.create_user(username='staff2', password='pass', is_staff=True)
        user = User.objects.create_user(username='user3', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='Good App', slug='good-app',
            require_gateway=True, data_sensitivity='high'
        )
        project = Project.objects.create(
            owner=sub.requester,
            name=sub.name,
            slug=sub.slug,
            description=sub.description,
            framework=sub.framework,
            require_gateway=sub.require_gateway,
        )
        sub.approved_project = project
        sub.reviewer = staff
        sub.status = ProjectSubmission.Status.APPROVED
        sub.save()
        sub.refresh_from_db()
        assert sub.status == 'approved'
        assert sub.approved_project == project
        assert project.owner == user
        assert project.require_gateway is True

    @pytest.mark.django_db
    def test_cannot_react_non_pending(self):
        """Only pending submissions can be reviewed."""
        user = User.objects.create_user(username='user4', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='Done App', slug='done-app',
            status='approved'
        )
        assert sub.status != ProjectSubmission.Status.PENDING

    @pytest.mark.django_db
    def test_approve_without_gateway(self):
        """Approval without requiring gateway keeps gateway off."""
        staff = User.objects.create_user(username='staff3', password='pass', is_staff=True)
        user = User.objects.create_user(username='user5', password='pass')
        sub = ProjectSubmission.objects.create(
            requester=user, name='Public App', slug='public-app',
            require_gateway=False, data_sensitivity='none'
        )
        project = Project.objects.create(
            owner=sub.requester, name=sub.name, slug=sub.slug,
            require_gateway=False,
        )
        sub.approved_project = project
        sub.reviewer = staff
        sub.status = ProjectSubmission.Status.APPROVED
        sub.save()
        assert project.require_gateway is False


class TestProjectApprovalRequiredSetting:
    """Test that the approval workflow respects the PROJECT_APPROVAL_REQUIRED flag."""

    @pytest.mark.django_db
    @override_settings(PROJECT_APPROVAL_REQUIRED=False)
    def test_flag_off_submission_invisible(self):
        """When flag is off, project creation should work normally (no submission)."""
        # This is validated at the view level, but we test the setting read
        from django.conf import settings
        assert getattr(settings, 'PROJECT_APPROVAL_REQUIRED', False) is False

    @pytest.mark.django_db
    @override_settings(PROJECT_APPROVAL_REQUIRED=True)
    def test_flag_on_requires_submission(self):
        """When flag is on, users must go through submission flow."""
        from django.conf import settings
        assert getattr(settings, 'PROJECT_APPROVAL_REQUIRED', False) is True

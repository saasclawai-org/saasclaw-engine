"""Tests for Project soft delete."""

from unittest.mock import Mock, patch

import pytest

from saasclaw_engine.projects.models import ActiveProjectManager, Project


@pytest.fixture
def active_project(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.create_user(username='testowner', password='pass')
    return Project.objects.create(
        name="Test Project",
        slug="test-soft-delete",
        owner=user,
    )


class TestSoftDelete:
    """Project soft delete preserves all data."""

    def test_soft_delete_sets_deleted_at(self, active_project):
        assert active_project.deleted_at is None
        active_project.soft_delete()
        active_project.refresh_from_db()
        assert active_project.deleted_at is not None
        assert active_project.status == Project.Status.ARCHIVED

    def test_soft_delete_excludes_from_default_manager(self, active_project):
        active_project.soft_delete()
        assert Project.objects.filter(slug="test-soft-delete").count() == 0
        assert Project.all_objects.filter(slug="test-soft-delete").count() == 1

    def test_restore_clears_deleted_at(self, active_project):
        active_project.soft_delete()
        active_project.refresh_from_db()
        assert active_project.is_deleted is True
        active_project.restore()
        active_project.refresh_from_db()
        assert active_project.deleted_at is None
        assert active_project.is_deleted is False

    def test_is_deleted_property(self, active_project):
        assert active_project.is_deleted is False
        active_project.soft_delete()
        active_project.refresh_from_db()
        assert active_project.is_deleted is True

    def test_get_project_or_404_skips_deleted(self, active_project):
        """Deleted projects should 404 via default manager."""
        from django.http import Http404
        active_project.soft_delete()
        with pytest.raises(Http404):
            from django.shortcuts import get_object_or_404
            get_object_or_404(Project, slug="test-soft-delete")

    def test_all_objects_still_accessible(self, active_project):
        """Admin queries via all_objects can still find soft-deleted projects."""
        active_project.soft_delete()
        found = Project.all_objects.get(slug="test-soft-delete")
        assert found.name == "Test Project"


class TestActiveProjectManager:
    """The default manager filters out soft-deleted projects."""

    def test_only_returns_active(self, db):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create_user(username='owner1', password='pass')
        p1 = Project.objects.create(name="Active", slug="active-mgr-1", owner=user)
        p2 = Project.objects.create(name="To Delete", slug="will-delete-mgr", owner=user)
        p2.soft_delete()
        active = list(Project.objects.all())
        slugs = [p.slug for p in active]
        assert "active-mgr-1" in slugs
        assert "will-delete-mgr" not in slugs

    def test_all_objects_unfiltered(self, db):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create_user(username='owner2', password='pass')
        p1 = Project.objects.create(name="Active", slug="active-mgr-2", owner=user)
        p2 = Project.objects.create(name="Deleted", slug="deleted-mgr-2", owner=user)
        p2.soft_delete()
        all_projects = list(Project.all_objects.all())
        assert len(all_projects) == 2

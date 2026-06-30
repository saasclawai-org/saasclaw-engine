"""Tests for the Database Console API.

Covers: table listing, table detail/schema browsing, SQL query execution,
read-only enforcement, environment switching, and permission checks.
"""

import json
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings

from saasclaw_engine.projects.models import Project

User = get_user_model()


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
    return Project.objects.create(
        owner=owner, name='Test Project', slug='test-project',
    )


class TestDbTables:
    """Tests for GET /api/db/{slug}/{env}/tables/ endpoint."""

    @pytest.mark.django_db
    def test_stranger_denied(self, project, stranger):
        factory = RequestFactory()
        request = factory.get(f'/api/db/{project.slug}/preview/tables/')
        request.user = stranger
        from saasclaw_engine.integrations.db_console import db_tables
        response = db_tables(request, project.slug, 'preview')
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_anonymous_denied(self, project):
        factory = RequestFactory()
        request = factory.get(f'/api/db/{project.slug}/preview/tables/')
        request.user = User.objects.create_user(username='anon', password='pass')
        from saasclaw_engine.integrations.db_console import db_tables
        response = db_tables(request, project.slug, 'preview')
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_invalid_environment_rejected(self, project, owner):
        factory = RequestFactory()
        request = factory.get(f'/api/db/{project.slug}/staging/tables/')
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_tables
        response = db_tables(request, project.slug, 'staging')
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_nonexistent_project_returns_404(self, owner):
        factory = RequestFactory()
        request = factory.get('/api/db/nonexistent/preview/tables/')
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_tables
        response = db_tables(request, 'nonexistent', 'preview')
        assert response.status_code == 404


class TestDbTableDetail:
    """Tests for GET /api/db/{slug}/{env}/table/{name}/ endpoint."""

    @pytest.mark.django_db
    def test_stranger_denied(self, project, stranger):
        factory = RequestFactory()
        request = factory.get(f'/api/db/{project.slug}/preview/table/users/')
        request.user = stranger
        from saasclaw_engine.integrations.db_console import db_table_detail
        response = db_table_detail(request, project.slug, 'preview', 'users')
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_invalid_environment_rejected(self, project, owner):
        factory = RequestFactory()
        request = factory.get(f'/api/db/{project.slug}/dev/table/users/')
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_table_detail
        response = db_table_detail(request, project.slug, 'dev', 'users')
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_sql_injection_in_table_name(self, project, owner):
        """Table names with SQL injection characters are rejected."""
        factory = RequestFactory()
        request = factory.get(f'/api/db/{project.slug}/preview/table/users; DROP TABLE users;/')
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_table_detail
        response = db_table_detail(request, project.slug, 'preview', 'users; DROP TABLE users;')
        assert response.status_code in (400, 500)  # Either rejected or SQL error


class TestDbQuery:
    """Tests for POST /api/db/{slug}/{env}/query/ endpoint."""

    @pytest.mark.django_db
    def test_stranger_denied(self, project, stranger):
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({'sql': 'SELECT 1'}),
            content_type='application/json',
        )
        request.user = stranger
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_invalid_environment_rejected(self, project, owner):
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/staging/query/',
            data=json.dumps({'sql': 'SELECT 1'}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'staging')
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_no_sql_returns_400(self, project, owner):
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_write_query_blocked_by_default(self, project, owner):
        """INSERT/UPDATE/DELETE are rejected unless write=true."""
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({'sql': 'INSERT INTO fake_table (x) VALUES (1)'}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_write_query_allowed_with_flag(self, project, owner):
        """INSERT with write=true should not be blocked by the write check."""
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({'sql': 'INSERT INTO nonexistent (x) VALUES (1)', 'write': True}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        # Will fail with error (no DB configured or table doesn't exist), but not 403
        assert response.status_code in (400, 500)  # SQL error or no DB, not permission error

    @pytest.mark.django_db
    def test_select_allowed_without_write_flag(self, project, owner):
        """SELECT queries pass without write=true."""
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({'sql': 'SELECT 1 AS val'}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        # May fail if no DB configured, but should not be blocked by write check
        assert response.status_code != 403

    @pytest.mark.django_db
    def test_delete_blocked_without_write_flag(self, project, owner):
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({'sql': 'DELETE FROM fake_table'}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_drop_blocked_without_write_flag(self, project, owner):
        factory = RequestFactory()
        request = factory.post(
            f'/api/db/{project.slug}/preview/query/',
            data=json.dumps({'sql': 'DROP TABLE fake_table'}),
            content_type='application/json',
        )
        request.user = owner
        from saasclaw_engine.integrations.db_console import db_query
        response = db_query(request, project.slug, 'preview')
        assert response.status_code == 403


class TestDbConsoleHelpers:
    """Tests for internal helper functions."""

    @pytest.mark.django_db
    def test_can_manage_owner(self, project, owner):
        assert _can_manage(owner, project) is True

    @pytest.mark.django_db
    def test_can_manage_staff(self, project, staff):
        assert _can_manage(staff, project) is True

    @pytest.mark.django_db
    def test_cannot_manage_stranger(self, project, stranger):
        assert _can_manage(stranger, project) is False

    @pytest.mark.django_db
    def test_cannot_manage_anonymous(self, project):
        anon = User.objects.create_user(username='anon99', password='pass')
        assert _can_manage(anon, project) is False

    @pytest.mark.django_db
    def test_cannot_manage_none_user(self, project):
        assert _can_manage(None, project) is False

    @pytest.mark.django_db
    def test_get_project_db_env_no_env_file(self, project):
        from saasclaw_engine.integrations.db_console import _get_project_db_env
        db_env, error = _get_project_db_env(project, 'preview')
        assert error is not None
        assert 'No database configured' in error

    @pytest.mark.django_db
    def test_get_project_db_env_with_env_file(self, project, tmp_path):
        """Simulate a .env file with database config."""
        from saasclaw_engine.integrations.db_console import _get_project_db_env
        import os
        from unittest.mock import patch
        from pathlib import Path

        # Mock the workspace_root to point to tmp_path
        env_dir = tmp_path / 'runtime' / 'preview'
        env_dir.mkdir(parents=True)
        (env_dir / '.env').write_text(
            'POSTGRES_DB=mydb\nPOSTGRES_USER=myuser\nPOSTGRES_PASSWORD=mypass\n'
            'POSTGRES_HOST=127.0.0.1\nPOSTGRES_PORT=5432\n'
        )

        # Mock workspace_root on the project
        with patch.object(project, 'workspace_root', str(tmp_path)):
            db_env, error = _get_project_db_env(project, 'preview')
            assert error is None
            assert db_env['dbname'] == 'mydb'
            assert db_env['user'] == 'myuser'
            assert db_env['password'] == 'mypass'
            assert db_env['host'] == '127.0.0.1'
            assert db_env['port'] == '5432'


def _can_manage(user, project):
    from saasclaw_engine.integrations.db_console import _can_manage
    return _can_manage(user, project)

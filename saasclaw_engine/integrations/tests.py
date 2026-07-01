"""Tests for integration models and webhook handling."""

import json

from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model

from saasclaw_engine.integrations.models import GitHubInstallation, InstallationRepository
from saasclaw_engine.integrations.views import github_webhook

User = get_user_model()


class GitHubInstallationModelTests(TestCase):
    """Tests for GitHubInstallation model."""

    def test_create_installation(self):
        user = User.objects.create_user(username='ghuser', password='pass')
        inst = GitHubInstallation.objects.create(
            user=user,
            account_name='my-org',
            account_type='Organization',
            installation_id=12345678,
            github_account_id=98765432,
        )
        self.assertEqual(inst.account_name, 'my-org')
        self.assertEqual(inst.account_type, 'Organization')
        self.assertEqual(inst.installation_id, 12345678)
        self.assertEqual(inst.github_account_id, 98765432)
        self.assertEqual(inst.access_metadata_json, {})
        self.assertEqual(str(inst), 'my-org (12345678)')

    def test_unique_installation_id(self):
        user = User.objects.create_user(username='ghuser2', password='pass')
        GitHubInstallation.objects.create(
            user=user, account_name='org1', installation_id=111,
        )
        with self.assertRaises(Exception):
            GitHubInstallation.objects.create(
                user=user, account_name='org2', installation_id=111,
            )

    def test_user_nullable(self):
        inst = GitHubInstallation.objects.create(
            account_name='system-install',
            installation_id=222,
        )
        self.assertIsNone(inst.user)
        self.assertEqual(inst.account_name, 'system-install')

    def test_access_metadata_json(self):
        user = User.objects.create_user(username='ghuser3', password='pass')
        inst = GitHubInstallation.objects.create(
            user=user, account_name='org-x', installation_id=333,
            access_metadata_json={'token': 'ghs_abc', 'expires_at': '2025-01-01'},
        )
        self.assertEqual(inst.access_metadata_json['token'], 'ghs_abc')

    def test_ordering(self):
        user = User.objects.create_user(username='ghuser4', password='pass')
        GitHubInstallation.objects.create(
            user=user, account_name='zebra', installation_id=444,
        )
        GitHubInstallation.objects.create(
            user=user, account_name='alpha', installation_id=555,
        )
        installs = list(GitHubInstallation.objects.all())
        self.assertEqual(installs[0].account_name, 'alpha')

    def test_github_account_id_nullable(self):
        user = User.objects.create_user(username='ghuser5', password='pass')
        inst = GitHubInstallation.objects.create(
            user=user, account_name='org', installation_id=666,
        )
        self.assertIsNone(inst.github_account_id)

    def test_user_reverse_relation(self):
        user = User.objects.create_user(username='ghuser6', password='pass')
        GitHubInstallation.objects.create(
            user=user, account_name='org', installation_id=777,
        )
        GitHubInstallation.objects.create(
            user=user, account_name='org2', installation_id=888,
        )
        self.assertEqual(user.github_installations.count(), 2)

    def test_sender_fields(self):
        inst = GitHubInstallation.objects.create(
            account_name='acme',
            installation_id=999,
            sender_github_id=12345,
            sender_login='octocat',
            repository_selection='selected',
        )
        self.assertEqual(inst.sender_github_id, 12345)
        self.assertEqual(inst.sender_login, 'octocat')
        self.assertEqual(inst.repository_selection, 'selected')


class InstallationRepositoryModelTests(TestCase):
    """Tests for InstallationRepository model."""

    def test_create_repo(self):
        inst = GitHubInstallation.objects.create(
            account_name='acme', installation_id=100,
        )
        repo = InstallationRepository.objects.create(
            installation=inst,
            repo_id=200,
            repo_name='my-project',
            full_name='acme/my-project',
            private=True,
            default_branch='main',
        )
        self.assertEqual(repo.full_name, 'acme/my-project')
        self.assertEqual(str(repo), 'acme/my-project')

    def test_unique_per_installation(self):
        inst = GitHubInstallation.objects.create(
            account_name='acme', installation_id=101,
        )
        InstallationRepository.objects.create(
            installation=inst, repo_id=201, repo_name='a', full_name='acme/a',
        )
        with self.assertRaises(Exception):
            InstallationRepository.objects.create(
                installation=inst, repo_id=201, repo_name='a-dup', full_name='acme/a',
            )

    def test_reverse_relation(self):
        inst = GitHubInstallation.objects.create(
            account_name='acme', installation_id=102,
        )
        InstallationRepository.objects.create(
            installation=inst, repo_id=301, repo_name='a', full_name='acme/a',
        )
        InstallationRepository.objects.create(
            installation=inst, repo_id=302, repo_name='b', full_name='acme/b',
        )
        self.assertEqual(inst.repos.count(), 2)


class GitHubWebhookViewTests(TestCase):
    """Tests for the GitHub webhook endpoint."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_webhook_requires_post(self):
        request = self.factory.get('/webhooks/github/')
        response = github_webhook(request)
        self.assertIn(response.status_code, (405, 403))

    def test_webhook_invalid_json_returns_bad_request(self):
        request = self.factory.post(
            '/webhooks/github/',
            data='not-json',
            content_type='text/plain',
            HTTP_X_GITHUB_EVENT='push',
        )
        response = github_webhook(request)
        self.assertEqual(response.status_code, 400)

    def test_webhook_no_secret_configured(self):
        from django.conf import settings
        old = settings.GITHUB_WEBHOOK_SECRET
        settings.GITHUB_WEBHOOK_SECRET = ''
        try:
            request = self.factory.post(
                '/webhooks/github/',
                data=json.dumps({}),
                content_type='application/json',
                HTTP_X_GITHUB_EVENT='push',
            )
            response = github_webhook(request)
            self.assertEqual(response.status_code, 400)
        finally:
            settings.GITHUB_WEBHOOK_SECRET = old

    def test_webhook_empty_body_treated_as_valid(self):
        from django.conf import settings
        old = settings.GITHUB_WEBHOOK_SECRET
        settings.GITHUB_WEBHOOK_SECRET = ''
        try:
            request = self.factory.post(
                '/webhooks/github/',
                data=b'',
                content_type='application/json',
                HTTP_X_GITHUB_EVENT='ping',
            )
            # Empty body → '{}' parsed, then hits GITHUB_WEBHOOK_SECRET check
            response = github_webhook(request)
            self.assertEqual(response.status_code, 400)
        finally:
            settings.GITHUB_WEBHOOK_SECRET = old

    def test_installation_event_creates_record(self):
        """Webhook creates installation and links to user by username."""
        User.objects.create_user(username='octocat', password='pass')
        payload = {
            'action': 'created',
            'installation': {
                'id': 900100,
                'account': {'login': 'acme-corp', 'type': 'Organization', 'id': 111},
                'repository_selection': 'selected',
                'repositories': [
                    {'id': 1, 'full_name': 'acme-corp/web-app', 'private': True, 'default_branch': 'main'},
                    {'id': 2, 'full_name': 'acme-corp/api', 'private': False, 'default_branch': 'develop'},
                ],
            },
            'sender': {'id': 55555, 'login': 'octocat'},
        }
        request = self.factory.post(
            '/webhooks/github/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_GITHUB_EVENT='installation',
        )
        response = github_webhook(request)
        self.assertEqual(response.status_code, 200)

        inst = GitHubInstallation.objects.get(installation_id=900100)
        self.assertEqual(inst.account_name, 'acme-corp')
        self.assertEqual(inst.sender_github_id, 55555)
        self.assertEqual(inst.sender_login, 'octocat')
        self.assertEqual(inst.repository_selection, 'selected')
        # Linked via username match
        self.assertEqual(inst.user.username, 'octocat')
        # Repos synced
        self.assertEqual(inst.repos.count(), 2)
        self.assertTrue(inst.repositories.filter(full_name='acme-corp/web-app').exists())
        self.assertTrue(inst.repositories.filter(full_name='acme-corp/api').exists())

    def test_installation_deleted_removes_record(self):
        GitHubInstallation.objects.create(
            account_name='acme', installation_id=99999,
        )
        payload = {
            'installation': {'id': 99999},
            'sender': {'id': 1, 'login': 'someone'},
        }
        request = self.factory.post(
            '/webhooks/github/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_GITHUB_EVENT='installation.deleted',
        )
        response = github_webhook(request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(GitHubInstallation.objects.filter(installation_id=99999).exists())

    def test_unlinked_installation_has_no_user(self):
        """Installation without a matching user stays user=None."""
        payload = {
            'action': 'created',
            'installation': {
                'id': 900200,
                'account': {'login': 'some-org', 'type': 'Organization', 'id': 222},
                'repository_selection': 'all',
                'repositories': [],
            },
            'sender': {'id': 99999, 'login': 'nonexistent_user'},
        }
        request = self.factory.post(
            '/webhooks/github/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_GITHUB_EVENT='installation',
        )
        response = github_webhook(request)
        self.assertEqual(response.status_code, 200)

        inst = GitHubInstallation.objects.get(installation_id=900200)
        self.assertIsNone(inst.user)
        self.assertEqual(inst.sender_login, 'nonexistent_user')

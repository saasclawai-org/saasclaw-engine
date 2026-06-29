"""Tests for integration models and webhook handling."""

import json

from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model

from saasclaw_engine.integrations.models import GitHubInstallation
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
        request = self.factory.post(
            '/webhooks/github/',
            data=json.dumps({}),
            content_type='application/json',
            HTTP_X_GITHUB_EVENT='push',
        )
        response = github_webhook(request)
        self.assertEqual(response.status_code, 400)

    def test_webhook_empty_body_treated_as_valid(self):
        request = self.factory.post(
            '/webhooks/github/',
            data=b'',
            content_type='application/json',
            HTTP_X_GITHUB_EVENT='ping',
        )
        # Empty body → '{}' parsed, then hits GITHUB_WEBHOOK_SECRET check
        response = github_webhook(request)
        self.assertEqual(response.status_code, 400)

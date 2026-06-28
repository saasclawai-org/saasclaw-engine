import subprocess
from unittest.mock import patch

from django.test import TestCase

from saasclaw_engine.integrations.github import clone_or_update_repo, commit_and_push_repo


class GitHubIntegrationTests(TestCase):
    @patch('integrations.github.time.sleep')
    @patch('integrations.github.create_installation_access_token', return_value='ghs-secret-token')
    @patch('integrations.github.subprocess.run')
    def test_clone_or_update_repo_retries_and_redacts_token_in_errors(self, mock_run, _mock_token, mock_sleep):
        branch_error = subprocess.CalledProcessError(
            128,
            ['git', 'clone', '--branch', 'main', 'https://x-access-token:ghs-secret-token@github.com/acme/demo.git', '/tmp/demo'],
            stderr='fatal: repository not ready yet',
        )
        clone_error = subprocess.CalledProcessError(
            128,
            ['git', 'clone', 'https://x-access-token:ghs-secret-token@github.com/acme/demo.git', '/tmp/demo'],
            stderr='fatal: repository not ready yet',
        )
        mock_run.side_effect = [branch_error, clone_error, branch_error, clone_error, None]

        destination = clone_or_update_repo(1, 'acme', 'demo', 'main', '/tmp/demo')

        self.assertEqual(destination, '/tmp/demo')
        self.assertEqual(mock_run.call_count, 5)
        self.assertEqual(mock_sleep.call_args_list, [((1,), {}), ((2,), {})])

    @patch('integrations.github.time.sleep')
    @patch('integrations.github.create_installation_access_token', return_value='ghs-secret-token')
    @patch('integrations.github.subprocess.run')
    def test_clone_or_update_repo_raises_sanitized_error_after_retries(self, mock_run, _mock_token, mock_sleep):
        branch_error = subprocess.CalledProcessError(
            128,
            ['git', 'clone', '--branch', 'main', 'https://x-access-token:ghs-secret-token@github.com/acme/demo.git', '/tmp/demo'],
            stderr='fatal: repository not found',
        )
        clone_error = subprocess.CalledProcessError(
            128,
            ['git', 'clone', 'https://x-access-token:ghs-secret-token@github.com/acme/demo.git', '/tmp/demo'],
            stderr='fatal: repository not found',
        )
        mock_run.side_effect = [branch_error, clone_error, branch_error, clone_error, branch_error, clone_error]

        with self.assertRaises(RuntimeError) as ctx:
            clone_or_update_repo(1, 'acme', 'demo', 'main', '/tmp/demo')

        message = str(ctx.exception)
        self.assertIn('Git clone failed for acme/demo', message)
        self.assertIn('fatal: repository not found', message)
        self.assertNotIn('ghs-secret-token', message)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('integrations.github.Path.exists', return_value=True)
    @patch('integrations.github.create_installation_access_token', return_value='ghs-secret-token')
    @patch('integrations.github.subprocess.run')
    def test_clone_or_update_repo_keeps_clean_remote_url(self, mock_run, _mock_token, _mock_exists):
        fetch_ok = subprocess.CompletedProcess(args=['git'], returncode=0, stdout='', stderr='')
        checkout_ok = subprocess.CompletedProcess(args=['git'], returncode=0, stdout='', stderr='')
        reset_ok = subprocess.CompletedProcess(args=['git'], returncode=0, stdout='', stderr='')
        mock_run.side_effect = [
            fetch_ok,
            fetch_ok,
            checkout_ok,
            reset_ok,
        ]

        destination = clone_or_update_repo(1, 'acme', 'demo', 'main', '/tmp/demo')

        self.assertEqual(destination, '/tmp/demo')
        set_url_command = mock_run.call_args_list[0].args[0]
        self.assertEqual(set_url_command, ['git', '-C', '/tmp/demo', 'remote', 'set-url', 'origin', 'https://github.com/acme/demo.git'])
        fetch_command = mock_run.call_args_list[1].args[0]
        self.assertIn('AUTHORIZATION: basic', fetch_command[2])
        self.assertNotIn('ghs-secret-token', ' '.join(set_url_command))

    @patch('integrations.github.create_installation_access_token', return_value='ghs-secret-token')
    @patch('integrations.github.subprocess.run')
    def test_commit_and_push_repo_uses_auth_header_without_persisting_token(self, mock_run, _mock_token):
        status_clean = subprocess.CompletedProcess(args=['git'], returncode=0, stdout=' M README.md\n', stderr='')
        push_ok = subprocess.CompletedProcess(args=['git'], returncode=0, stdout='', stderr='')
        mock_run.side_effect = [
            push_ok,
            push_ok,
            push_ok,
            status_clean,
            push_ok,
            push_ok,
            push_ok,
        ]

        commit_and_push_repo(1, 'acme', 'demo', 'main', '/tmp/demo', 'test commit')

        set_url_command = mock_run.call_args_list[5].args[0]
        self.assertEqual(set_url_command, ['git', '-C', '/tmp/demo', 'remote', 'set-url', 'origin', 'https://github.com/acme/demo.git'])
        push_command = mock_run.call_args_list[6].args[0]
        self.assertIn('AUTHORIZATION: basic', push_command[2])
        self.assertNotIn('ghs-secret-token', ' '.join(set_url_command))

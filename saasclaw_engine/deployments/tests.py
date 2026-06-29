"""Tests for deployment models — environments, deployments, custom domains, env vars."""

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone as dj_timezone

from saasclaw_engine.projects.models import Project
from saasclaw_engine.deployments.models import (
    CustomDomain,
    Deployment,
    Environment,
    EnvironmentVariable,
)

User = get_user_model()


class EnvironmentModelTests(TestCase):
    """Tests for the Environment model."""

    def setUp(self):
        self.user = User.objects.create_user(username='envuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Env Project', slug='env-project', framework='html'
        )

    def test_create_preview_environment(self):
        env = Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='env-project.preview.saasclaw.ai',
        )
        self.assertEqual(env.name, 'preview')
        self.assertEqual(env.runtime_kind, 'static')
        self.assertEqual(env.is_primary, False)
        self.assertEqual(str(env), 'env-project:preview')

    def test_create_production_environment(self):
        env = Environment.objects.create(
            project=self.project, name='production', slug='env-project-production',
            domain='env-project.saasclaw.ai', is_primary=True,
        )
        self.assertEqual(env.name, 'production')
        self.assertTrue(env.is_primary)

    def test_unique_project_name_constraint(self):
        Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='preview.example.com',
        )
        with self.assertRaises(Exception):
            Environment.objects.create(
                project=self.project, name='preview', slug='other-slug',
                domain='other.example.com',
            )

    def test_same_name_different_project_allowed(self):
        user2 = User.objects.create_user(username='envuser2', password='pass')
        p2 = Project.objects.create(owner=user2, name='P2', slug='p2', framework='html')
        Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='a.example.com',
        )
        env2 = Environment.objects.create(
            project=p2, name='preview', slug='p2-preview', domain='b.example.com',
        )
        self.assertEqual(env2.name, 'preview')

    def test_runtime_kind_choices(self):
        env = Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='x.example.com', runtime_kind='node_ssr',
        )
        self.assertEqual(env.runtime_kind, 'node_ssr')

    def test_app_port_nullable(self):
        env = Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='x.example.com',
        )
        self.assertIsNone(env.app_port)

    def test_ordering(self):
        e1 = Environment.objects.create(
            project=self.project, name='production', slug='env-project-prod',
            domain='prod.example.com',
        )
        e2 = Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='prev.example.com',
        )
        envs = list(Environment.objects.filter(project=self.project))
        self.assertEqual(envs[0], e2)  # preview comes first alphabetically

    def test_environment_has_deployment_reverse_relation(self):
        env = Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='x.example.com',
        )
        self.assertEqual(env.deployments.count(), 0)

    def test_environment_has_variables_reverse_relation(self):
        env = Environment.objects.create(
            project=self.project, name='preview', slug='env-project-preview',
            domain='x.example.com',
        )
        self.assertEqual(env.variables.count(), 0)


class DeploymentModelTests(TestCase):
    """Tests for the Deployment model."""

    def setUp(self):
        self.user = User.objects.create_user(username='deployuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Deploy Project', slug='deploy-project',
            framework='html',
        )
        self.environment = Environment.objects.create(
            project=self.project, name='preview', slug='deploy-project-preview',
            domain='deploy-project.preview.saasclaw.ai',
        )

    def test_create_deployment_defaults(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
        )
        self.assertEqual(dep.status, 'queued')
        self.assertEqual(dep.source, 'manual')
        self.assertEqual(dep.git_branch, '')
        self.assertEqual(dep.metadata_json, {})

    def test_create_deployment_with_user(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            triggered_by=self.user, source='agent',
        )
        self.assertEqual(dep.triggered_by, self.user)
        self.assertEqual(dep.source, 'agent')

    def test_create_deployment_with_git_info(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            git_branch='feature/new-ui',
            git_commit_sha='abc123def456',
            git_commit_message='Add new landing page',
        )
        self.assertEqual(dep.git_branch, 'feature/new-ui')
        self.assertEqual(dep.git_commit_sha, 'abc123def456')
        self.assertEqual(dep.git_commit_message, 'Add new landing page')

    def test_deployment_str(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
        )
        self.assertEqual(str(dep), 'deploy-project:preview:queued')

    def test_status_transitions(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
        )
        self.assertEqual(dep.status, 'queued')

        dep.status = 'running'
        dep.started_at = dj_timezone.now()
        dep.save()
        self.assertEqual(dep.status, 'running')

        dep.status = 'succeeded'
        dep.finished_at = dj_timezone.now()
        dep.save()
        self.assertEqual(dep.status, 'succeeded')

    def test_failed_deployment_stores_error(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            status='failed',
            error_message='Build command exited with code 1',
        )
        dep.save()
        self.assertEqual(dep.error_message, 'Build command exited with code 1')

    def test_canceled_deployment(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            status='canceled',
        )
        self.assertEqual(dep.status, 'canceled')

    def test_deployment_ordering_newest_first(self):
        dep1 = Deployment.objects.create(
            project=self.project, environment=self.environment,
        )
        dep2 = Deployment.objects.create(
            project=self.project, environment=self.environment,
        )
        deps = list(Deployment.objects.all())
        self.assertEqual(deps[0], dep2)
        self.assertEqual(deps[1], dep1)

    def test_deployment_metadata_json(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            metadata_json={'build_time': 42, 'cache_hit': True},
        )
        dep.save()
        self.assertEqual(dep.metadata_json['build_time'], 42)
        self.assertTrue(dep.metadata_json['cache_hit'])

    def test_triggered_by_nullable(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            source='system',
        )
        self.assertIsNone(dep.triggered_by)
        self.assertEqual(dep.source, 'system')

    def test_git_push_source(self):
        dep = Deployment.objects.create(
            project=self.project, environment=self.environment,
            source='git_push',
        )
        self.assertEqual(dep.source, 'git_push')

    def test_project_reverse_relation(self):
        Deployment.objects.create(
            project=self.project, environment=self.environment,
        )
        self.assertEqual(self.project.deployments.count(), 1)


class CustomDomainModelTests(TestCase):
    """Tests for the CustomDomain model."""

    def setUp(self):
        self.user = User.objects.create_user(username='domainuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Domain Project', slug='domain-project',
            framework='html',
        )
        self.environment = Environment.objects.create(
            project=self.project, name='production', slug='domain-project-prod',
            domain='domain-project.saasclaw.ai',
        )

    def test_create_custom_domain(self):
        cd = CustomDomain.objects.create(
            project=self.project,
            domain='www.myapp.com',
        )
        self.assertEqual(cd.status, 'pending_dns')
        self.assertEqual(str(cd), 'www.myapp.com (pending_dns)')

    def test_unique_domain_constraint(self):
        CustomDomain.objects.create(project=self.project, domain='myapp.com')
        with self.assertRaises(Exception):
            CustomDomain.objects.create(project=self.project, domain='myapp.com')

    def test_status_transitions(self):
        cd = CustomDomain.objects.create(
            project=self.project, domain='app.example.com',
        )
        cd.status = 'verifying'
        cd.save()
        cd.status = 'ssl_requesting'
        cd.save()
        cd.status = 'active'
        cd.dns_verified_at = dj_timezone.now()
        cd.ssl_cert_path = '/etc/letsencrypt/live/app.example.com/fullchain.pem'
        cd.save()
        self.assertEqual(cd.status, 'active')
        self.assertIsNotNone(cd.dns_verified_at)

    def test_failed_status_with_error(self):
        cd = CustomDomain.objects.create(
            project=self.project, domain='fail.example.com',
            status='failed', error_message='DNS validation timed out',
        )
        self.assertEqual(cd.error_message, 'DNS validation timed out')

    def test_project_reverse_relation(self):
        CustomDomain.objects.create(project=self.project, domain='a.com')
        CustomDomain.objects.create(project=self.project, domain='b.com')
        self.assertEqual(self.project.custom_domains.count(), 2)

    def test_ordering_newest_first(self):
        cd1 = CustomDomain.objects.create(project=self.project, domain='first.com')
        cd2 = CustomDomain.objects.create(project=self.project, domain='second.com')
        domains = list(CustomDomain.objects.all())
        self.assertEqual(domains[0], cd2)


class EnvironmentVariableModelTests(TestCase):
    """Tests for the EnvironmentVariable model."""

    def setUp(self):
        self.user = User.objects.create_user(username='envvaruser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='EV Project', slug='ev-project', framework='html',
        )
        self.env = Environment.objects.create(
            project=self.project, name='preview', slug='ev-project-preview',
            domain='ev-project.preview.saasclaw.ai',
        )

    def test_create_env_var(self):
        ev = EnvironmentVariable.objects.create(
            project=self.project, environment=self.env,
            key='DB_HOST', value='localhost',
        )
        self.assertEqual(ev.key, 'DB_HOST')
        self.assertEqual(ev.value, 'localhost')
        self.assertFalse(ev.is_secret)

    def test_secret_var_masks_value_in_str(self):
        ev = EnvironmentVariable.objects.create(
            project=self.project, environment=self.env,
            key='SECRET_KEY', value='super-secret-value-12345',
            is_secret=True,
        )
        self.assertIn('***', str(ev))
        self.assertNotIn('super-secret-value-12345', str(ev))

    def test_non_secret_var_shows_value_in_str(self):
        ev = EnvironmentVariable.objects.create(
            project=self.project, environment=self.env,
            key='APP_NAME', value='my-application',
            is_secret=False,
        )
        self.assertIn('my-application', str(ev))

    def test_secret_var_short_value_masking(self):
        ev = EnvironmentVariable.objects.create(
            project=self.project, environment=self.env,
            key='KEY', value='ab',
            is_secret=True,
        )
        s = str(ev)
        self.assertIn('***', s)
        self.assertNotIn('ab', s)

    def test_unique_environment_key_constraint(self):
        EnvironmentVariable.objects.create(
            project=self.project, environment=self.env, key='PORT', value='8080',
        )
        with self.assertRaises(Exception):
            EnvironmentVariable.objects.create(
                project=self.project, environment=self.env, key='PORT', value='9090',
            )

    def test_same_key_different_environment_allowed(self):
        env2 = Environment.objects.create(
            project=self.project, name='production', slug='ev-project-prod',
            domain='ev-project.saasclaw.ai',
        )
        EnvironmentVariable.objects.create(
            project=self.project, environment=self.env, key='PORT', value='8080',
        )
        ev2 = EnvironmentVariable.objects.create(
            project=self.project, environment=env2, key='PORT', value='3000',
        )
        self.assertEqual(ev2.value, '3000')

    def test_ordering_by_key(self):
        EnvironmentVariable.objects.create(
            project=self.project, environment=self.env, key='ZZZ', value='last',
        )
        EnvironmentVariable.objects.create(
            project=self.project, environment=self.env, key='AAA', value='first',
        )
        keys = [ev.key for ev in EnvironmentVariable.objects.filter(environment=self.env)]
        self.assertEqual(keys, ['AAA', 'ZZZ'])

    def test_blank_value_allowed(self):
        ev = EnvironmentVariable.objects.create(
            project=self.project, environment=self.env,
            key='EMPTY_VAR', value='',
        )
        self.assertEqual(ev.value, '')

    def test_long_value(self):
        long_val = 'x' * 10000
        ev = EnvironmentVariable.objects.create(
            project=self.project, environment=self.env,
            key='LONG_VAL', value=long_val,
        )
        self.assertEqual(len(ev.value), 10000)

    def test_project_reverse_relation(self):
        EnvironmentVariable.objects.create(
            project=self.project, environment=self.env, key='K', value='v',
        )
        self.assertEqual(self.project.env_variables.count(), 1)

    def test_environment_reverse_relation(self):
        EnvironmentVariable.objects.create(
            project=self.project, environment=self.env, key='K', value='v',
        )
        self.assertEqual(self.env.variables.count(), 1)


class DeploymentViewTests(TestCase):
    """Tests for deployment views (currently placeholder)."""

    def test_views_module_exists(self):
        from saasclaw_engine.deployments import views
        self.assertTrue(hasattr(views, 'render'))

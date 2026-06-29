"""Tests for studio models — ProviderKey, Workspace, AgentProfile, Todo, sessions, messages, TokenUsage."""

import uuid

from django.test import TestCase
from django.contrib.auth import get_user_model

from saasclaw_engine.projects.models import Project
from saasclaw_engine.studio_models.models import (
    AgentMessage,
    AgentProfile,
    AgentSession,
    ProviderKey,
    TokenUsage,
    Todo,
    Workspace,
)

User = get_user_model()


class ProviderKeyModelTests(TestCase):
    """Tests for ProviderKey model."""

    def setUp(self):
        self.user = User.objects.create_user(username='pkuser', password='pass')

    def test_create_provider_key(self):
        pk = ProviderKey.objects.create(
            user=self.user, provider='openai',
            api_key='sk-proj-abc123def456ghi789jkl',
            default_model='gpt-5.4',
        )
        self.assertEqual(pk.provider, 'openai')
        self.assertEqual(pk.default_model, 'gpt-5.4')
        self.assertTrue(pk.is_active)

    def test_str_masks_api_key(self):
        pk = ProviderKey.objects.create(
            user=self.user, provider='anthropic',
            api_key='sk-ant-api03-very-long-secret-key-value-here',
        )
        s = str(pk)
        self.assertIn('anthropic:', s)
        self.assertIn('...', s)
        # First 8 chars should be visible
        self.assertIn('sk-ant-ap', s)

    def test_str_short_key_shows_asterisks(self):
        pk = ProviderKey.objects.create(
            user=self.user, provider='zai',
            api_key='short',
        )
        s = str(pk)
        self.assertIn('***', s)
        self.assertNotIn('short', s)

    def test_unique_user_provider(self):
        ProviderKey.objects.create(
            user=self.user, provider='openai', api_key='sk-key1',
        )
        with self.assertRaises(Exception):
            ProviderKey.objects.create(
                user=self.user, provider='openai', api_key='sk-key2',
            )

    def test_same_provider_different_users(self):
        user2 = User.objects.create_user(username='pkuser2', password='pass')
        ProviderKey.objects.create(user=self.user, provider='zai', api_key='key1')
        pk2 = ProviderKey.objects.create(user=user2, provider='zai', api_key='key2')
        self.assertEqual(pk2.provider, 'zai')

    def test_deactivate_key(self):
        pk = ProviderKey.objects.create(
            user=self.user, provider='openai', api_key='sk-key',
        )
        pk.is_active = False
        pk.save()
        self.assertFalse(pk.is_active)

    def test_blank_default_model(self):
        pk = ProviderKey.objects.create(
            user=self.user, provider='anthropic', api_key='sk-key',
            default_model='',
        )
        self.assertEqual(pk.default_model, '')

    def test_ordering_by_provider(self):
        ProviderKey.objects.create(
            user=self.user, provider='zai', api_key='key1',
        )
        ProviderKey.objects.create(
            user=self.user, provider='openai', api_key='key2',
        )
        ProviderKey.objects.create(
            user=self.user, provider='anthropic', api_key='key3',
        )
        providers = [pk.provider for pk in self.user.provider_keys.all()]
        self.assertEqual(providers, ['anthropic', 'openai', 'zai'])

    def test_provider_choices(self):
        valid_providers = [c[0] for c in ProviderKey.provider.field.choices]
        for p in ['zai', 'openai', 'anthropic']:
            self.assertIn(p, valid_providers)


class WorkspaceModelTests(TestCase):
    """Tests for Workspace model."""

    def setUp(self):
        self.user = User.objects.create_user(username='wsuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='WS Project', slug='ws-project', framework='html',
        )

    def test_create_workspace(self):
        ws = Workspace.objects.create(
            project=self.project, user=self.user,
        )
        self.assertEqual(ws.base_branch, 'main')
        self.assertEqual(ws.work_branch, '')
        self.assertTrue(ws.is_active)
        self.assertIsInstance(ws.id, uuid.UUID)
        self.assertEqual(str(ws), 'ws-project @ main')

    def test_branch_property(self):
        ws = Workspace.objects.create(
            project=self.project, user=self.user,
        )
        self.assertEqual(ws.branch, 'main')  # falls back to base_branch

    def test_branch_property_with_work_branch(self):
        ws = Workspace.objects.create(
            project=self.project, user=self.user,
            work_branch='feature/new-ui',
        )
        self.assertEqual(ws.branch, 'feature/new-ui')

    def test_project_reverse_relation(self):
        Workspace.objects.create(project=self.project, user=self.user)
        self.assertEqual(self.project.workspaces.count(), 1)

    def test_user_reverse_relation(self):
        Workspace.objects.create(project=self.project, user=self.user)
        self.assertEqual(self.user.workspaces.count(), 1)

    def test_ordering(self):
        ws1 = Workspace.objects.create(project=self.project, user=self.user)
        ws2 = Workspace.objects.create(project=self.project, user=self.user)
        workspaces = list(Workspace.objects.all())
        self.assertEqual(workspaces[0], ws2)

    def test_blank_local_path(self):
        ws = Workspace.objects.create(
            project=self.project, user=self.user,
        )
        self.assertEqual(ws.local_path, '')


class AgentProfileModelTests(TestCase):
    """Tests for AgentProfile model."""

    def test_create_profile(self):
        profile = AgentProfile.objects.create(
            name='Code Helper',
            emoji='🤖',
            description='Helps with coding tasks',
            system_prompt='You are a helpful coding assistant.',
        )
        self.assertEqual(profile.name, 'Code Helper')
        self.assertEqual(profile.emoji, '🤖')
        self.assertEqual(profile.suggested_provider, '')
        self.assertEqual(profile.suggested_model, '')
        self.assertFalse(profile.is_default)
        self.assertIsInstance(profile.id, uuid.UUID)
        self.assertEqual(str(profile), '🤖 Code Helper')

    def test_allowed_tools_json(self):
        profile = AgentProfile.objects.create(
            name='Limited Agent',
            allowed_tools=['read_file', 'write_file'],
        )
        self.assertEqual(profile.allowed_tools, ['read_file', 'write_file'])

    def test_empty_allowed_tools(self):
        profile = AgentProfile.objects.create(name='All Tools Agent')
        self.assertEqual(profile.allowed_tools, [])

    def test_suggested_provider_and_model(self):
        profile = AgentProfile.objects.create(
            name='Fast Agent',
            suggested_provider='openai',
            suggested_model='gpt-5.4',
        )
        self.assertEqual(profile.suggested_provider, 'openai')
        self.assertEqual(profile.suggested_model, 'gpt-5.4')

    def test_ordering(self):
        AgentProfile.objects.create(name='Z Agent', order=10)
        AgentProfile.objects.create(name='A Agent', order=0)
        profiles = list(AgentProfile.objects.all())
        self.assertEqual(profiles[0].name, 'A Agent')
        self.assertEqual(profiles[1].name, 'Z Agent')

    def test_default_emoji(self):
        profile = AgentProfile.objects.create(name='Default Emoji')
        self.assertEqual(profile.emoji, '🏗️')

    def test_system_prompt_blank(self):
        profile = AgentProfile.objects.create(name='No Prompt')
        self.assertEqual(profile.system_prompt, '')


class TodoModelTests(TestCase):
    """Tests for Todo model."""

    def setUp(self):
        self.user = User.objects.create_user(username='todouser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Todo Project', slug='todo-project',
            framework='html',
        )

    def test_create_todo(self):
        todo = Todo.objects.create(
            project=self.project, text='Implement login page',
        )
        self.assertFalse(todo.done)
        self.assertEqual(todo.order, 0)
        self.assertIsInstance(todo.id, uuid.UUID)

    def test_mark_done(self):
        todo = Todo.objects.create(
            project=self.project, text='Set up database',
        )
        todo.done = True
        todo.save()
        self.assertTrue(todo.done)

    def test_ordering(self):
        t1 = Todo.objects.create(project=self.project, text='First', order=2)
        t2 = Todo.objects.create(project=self.project, text='Second', order=1)
        todos = list(Todo.objects.filter(project=self.project))
        self.assertEqual(todos[0], t2)

    def test_project_reverse_relation(self):
        Todo.objects.create(project=self.project, text='Task 1')
        Todo.objects.create(project=self.project, text='Task 2')
        self.assertEqual(self.project.todos.count(), 2)

    def test_long_text(self):
        long_text = 'A' * 500
        todo = Todo.objects.create(project=self.project, text=long_text)
        self.assertEqual(len(todo.text), 500)


class AgentSessionModelTests(TestCase):
    """Tests for AgentSession model."""

    def setUp(self):
        self.user = User.objects.create_user(username='sessuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Sess Project', slug='sess-project',
            framework='html',
        )
        self.profile = AgentProfile.objects.create(name='Builder')

    def test_create_session(self):
        session = AgentSession.objects.create(
            project=self.project, user=self.user,
            title='Build a landing page',
        )
        self.assertEqual(session.title, 'Build a landing page')
        self.assertEqual(session.status, 'idle')
        self.assertEqual(session.stage, 'chat')
        self.assertIsNone(session.profile)
        self.assertIsNone(session.completed_at)
        self.assertIsInstance(session.id, uuid.UUID)

    def test_session_with_profile(self):
        session = AgentSession.objects.create(
            project=self.project, user=self.user,
            title='Edit code', profile=self.profile,
        )
        self.assertEqual(session.profile, self.profile)

    def test_session_str_with_title(self):
        session = AgentSession.objects.create(
            project=self.project, user=self.user,
            title='My Session',
        )
        self.assertEqual(str(session), 'My Session')

    def test_session_str_without_title(self):
        session = AgentSession.objects.create(
            project=self.project, user=self.user,
        )
        self.assertIn(session.id.hex[:8], str(session))

    def test_session_status_transitions(self):
        session = AgentSession.objects.create(
            project=self.project, user=self.user,
            title='Task',
        )
        session.status = 'running'
        session.save()
        session.status = 'ended'
        session.completed_at = dj_now()
        session.save()
        self.assertEqual(session.status, 'ended')

    def test_session_with_workspace(self):
        ws = Workspace.objects.create(project=self.project, user=self.user)
        session = AgentSession.objects.create(
            project=self.project, user=self.user,
            workspace=ws,
        )
        self.assertEqual(session.workspace, ws)

    def test_profile_reverse_relation(self):
        AgentSession.objects.create(
            project=self.project, user=self.user,
            profile=self.profile, title='S1',
        )
        AgentSession.objects.create(
            project=self.project, user=self.user,
            profile=self.profile, title='S2',
        )
        self.assertEqual(self.profile.sessions.count(), 2)

    def test_ordering_newest_updated_first(self):
        s1 = AgentSession.objects.create(project=self.project, user=self.user)
        s2 = AgentSession.objects.create(project=self.project, user=self.user)
        sessions = list(AgentSession.objects.all())
        self.assertEqual(sessions[0], s2)


def dj_now():
    """Helper to import timezone.now without a top-level import that might shadow."""
    from django.utils import timezone
    return timezone.now()


class AgentMessageModelTests(TestCase):
    """Tests for AgentMessage model."""

    def setUp(self):
        self.user = User.objects.create_user(username='msguser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Msg Project', slug='msg-project',
            framework='html',
        )
        self.session = AgentSession.objects.create(
            project=self.project, user=self.user,
            title='Chat Session',
        )

    def test_create_user_message(self):
        msg = AgentMessage.objects.create(
            session=self.session, role='user',
            content='Build me a todo app',
        )
        self.assertEqual(msg.role, 'user')
        self.assertEqual(msg.tool_call, {})

    def test_create_assistant_message(self):
        msg = AgentMessage.objects.create(
            session=self.session, role='assistant',
            content='Here is your todo app code...',
        )
        self.assertEqual(msg.role, 'assistant')

    def test_create_tool_message(self):
        msg = AgentMessage.objects.create(
            session=self.session, role='tool',
            content='File written: todo.html',
        )
        self.assertEqual(msg.role, 'tool')

    def test_create_system_message(self):
        msg = AgentMessage.objects.create(
            session=self.session, role='system',
            content='Deploy started',
        )
        self.assertEqual(msg.role, 'system')

    def test_str_truncates_long_content(self):
        long_content = 'x' * 200
        msg = AgentMessage.objects.create(
            session=self.session, role='assistant',
            content=long_content,
        )
        s = str(msg)
        self.assertIn('assistant:', s)
        self.assertTrue(len(s) < 250)

    def test_tool_call_json(self):
        msg = AgentMessage.objects.create(
            session=self.session, role='assistant',
            content='Writing file...',
            tool_call={'name': 'write_file', 'args': {'path': '/index.html'}},
        )
        self.assertEqual(msg.tool_call['name'], 'write_file')

    def test_message_ordering(self):
        m1 = AgentMessage.objects.create(
            session=self.session, role='user', content='Hello',
        )
        m2 = AgentMessage.objects.create(
            session=self.session, role='assistant', content='Hi',
        )
        messages = list(self.session.messages.all())
        self.assertEqual(messages[0], m1)
        self.assertEqual(messages[1], m2)

    def test_session_reverse_relation(self):
        AgentMessage.objects.create(
            session=self.session, role='user', content='Msg 1',
        )
        AgentMessage.objects.create(
            session=self.session, role='assistant', content='Msg 2',
        )
        self.assertEqual(self.session.messages.count(), 2)

    def test_role_choices(self):
        expected = ['user', 'assistant', 'tool', 'system']
        actual = [c[0] for c in AgentMessage.role.field.choices]
        for r in expected:
            self.assertIn(r, actual)


class TokenUsageModelTests(TestCase):
    """Tests for TokenUsage model."""

    def setUp(self):
        self.user = User.objects.create_user(username='tokenuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Token Project', slug='token-project',
            framework='html',
        )
        self.session = AgentSession.objects.create(
            project=self.project, user=self.user,
            title='Token Session',
        )

    def test_create_token_usage(self):
        tu = TokenUsage.objects.create(
            project=self.project, session=self.session, user=self.user,
            provider='openai', model='gpt-5.4',
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            cost_usd='0.001500',
        )
        self.assertEqual(tu.total_tokens, 150)
        self.assertEqual(tu.cost_usd, 0.0015)
        self.assertEqual(str(tu), 'openai/gpt-5.4 — 150 tokens')

    def test_nullable_session(self):
        tu = TokenUsage.objects.create(
            project=self.project, user=self.user,
            provider='zai', model='glm-5.2',
            total_tokens=200,
        )
        self.assertIsNone(tu.session)

    def test_nullable_user(self):
        tu = TokenUsage.objects.create(
            project=self.project, session=self.session,
            provider='anthropic', model='claude-sonnet-4-20250514',
            total_tokens=300,
        )
        self.assertIsNone(tu.user)

    def test_ordering_newest_first(self):
        tu1 = TokenUsage.objects.create(
            project=self.project, user=self.user,
            provider='openai', model='gpt-5.4', total_tokens=100,
        )
        tu2 = TokenUsage.objects.create(
            project=self.project, user=self.user,
            provider='openai', model='gpt-5.4', total_tokens=200,
        )
        usages = list(TokenUsage.objects.all())
        self.assertEqual(usages[0], tu2)

    def test_profile_and_stage_fields(self):
        tu = TokenUsage.objects.create(
            project=self.project, user=self.user,
            provider='openai', model='gpt-5.4',
            total_tokens=100, profile='builder', stage='chat',
        )
        self.assertEqual(tu.profile, 'builder')
        self.assertEqual(tu.stage, 'chat')

    def test_zero_tokens(self):
        tu = TokenUsage.objects.create(
            project=self.project, user=self.user,
            provider='zai', model='glm-5.2',
        )
        self.assertEqual(tu.total_tokens, 0)
        self.assertEqual(tu.cost_usd, 0)

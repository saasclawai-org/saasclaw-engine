"""Tests for agent task models — creation, status lifecycle, attachments, threading."""

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone as dj_timezone

from saasclaw_engine.projects.models import Project
from saasclaw_engine.agents.models import AgentTask, AgentTaskAttachment

User = get_user_model()


class AgentTaskModelTests(TestCase):
    """Tests for AgentTask creation and properties."""

    def setUp(self):
        self.user = User.objects.create_user(username='taskuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Task Project', slug='task-project', framework='html',
        )

    def test_create_task_defaults(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Fix the layout',
        )
        self.assertEqual(task.status, 'queued')
        self.assertEqual(task.task_type, 'edit_code')
        self.assertEqual(task.prompt, 'Fix the layout')
        self.assertEqual(task.metadata_json, {})
        self.assertEqual(task.error_message, '')
        self.assertIsNone(task.started_at)
        self.assertIsNone(task.finished_at)

    def test_task_str(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='Plan it',
        )
        self.assertEqual(str(task), 'task-project:plan:queued')

    def test_all_task_type_choices_valid(self):
        expected = [
            'plan', 'edit_code', 'create_resource', 'generate_site',
            'fix_bug', 'inspect_repo', 'deploy_preview', 'deploy_production',
        ]
        actual = [c[0] for c in AgentTask.TaskType.choices]
        for t in expected:
            self.assertIn(t, actual)

    def test_all_status_choices(self):
        expected = ['queued', 'running', 'succeeded', 'failed', 'canceled']
        actual = [c[0] for c in AgentTask.Status.choices]
        for s in expected:
            self.assertIn(s, actual)

    def test_task_scoped_to_project(self):
        user2 = User.objects.create_user(username='taskuser2', password='pass')
        p2 = Project.objects.create(owner=user2, name='P2', slug='p2', framework='html')
        AgentTask.objects.create(
            project=self.project, requested_by=self.user, task_type='plan', prompt='A',
        )
        AgentTask.objects.create(
            project=p2, requested_by=user2, task_type='plan', prompt='B',
        )
        self.assertEqual(self.project.agent_tasks.count(), 1)
        self.assertEqual(p2.agent_tasks.count(), 1)

    def test_user_reverse_relation(self):
        AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='generate_site', prompt='Build it',
        )
        self.assertEqual(self.user.agent_tasks_requested.count(), 1)


class AgentTaskStatusTests(TestCase):
    """Tests for AgentTask status lifecycle."""

    def setUp(self):
        self.user = User.objects.create_user(username='statususer', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Status Project', slug='status-project',
            framework='html',
        )

    def test_full_success_lifecycle(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Edit code',
        )
        self.assertEqual(task.status, 'queued')

        task.status = 'running'
        task.started_at = dj_timezone.now()
        task.save()
        self.assertEqual(task.status, 'running')

        task.status = 'succeeded'
        task.result_summary = 'Layout fixed successfully'
        task.finished_at = dj_timezone.now()
        task.save()
        self.assertEqual(task.status, 'succeeded')
        self.assertEqual(task.result_summary, 'Layout fixed successfully')

    def test_failure_lifecycle(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='fix_bug', prompt='Fix bug',
        )
        task.status = 'running'
        task.started_at = dj_timezone.now()
        task.save()

        task.status = 'failed'
        task.error_message = 'Timeout after 60s'
        task.finished_at = dj_timezone.now()
        task.save()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_message, 'Timeout after 60s')

    def test_cancel_lifecycle(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='Plan',
        )
        task.status = 'canceled'
        task.finished_at = dj_timezone.now()
        task.save()
        self.assertEqual(task.status, 'canceled')


class AgentTaskThreadingTests(TestCase):
    """Tests for task parent/child and thread relationships."""

    def setUp(self):
        self.user = User.objects.create_user(username='threaduser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Thread Project', slug='thread-project',
            framework='html',
        )

    def test_parent_child_relationship(self):
        parent = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='Plan a feature',
        )
        child = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Implement it',
            parent_task=parent,
        )
        self.assertEqual(child.parent_task, parent)
        self.assertEqual(parent.followups.count(), 1)
        self.assertIn(child, parent.followups.all())

    def test_thread_key_groups_tasks(self):
        thread_key = 'thread-abc-123'
        t1 = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='Step 1', thread_key=thread_key,
        )
        t2 = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Step 2', thread_key=thread_key,
        )
        same_thread = AgentTask.objects.filter(thread_key=thread_key)
        self.assertEqual(same_thread.count(), 2)
        self.assertIn(t1, same_thread)
        self.assertIn(t2, same_thread)

    def test_parent_deletion_keeps_children(self):
        parent = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='Plan',
        )
        child = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Edit',
            parent_task=parent,
        )
        parent.delete()
        child.refresh_from_db()
        self.assertIsNone(child.parent_task)

    def test_blank_thread_key(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='inspect_repo', prompt='Inspect',
        )
        self.assertEqual(task.thread_key, '')

    def test_blank_linked_branch_and_sha(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Edit',
        )
        self.assertEqual(task.linked_branch, '')
        self.assertEqual(task.linked_commit_sha, '')


class AgentTaskGitFieldsTests(TestCase):
    """Tests for AgentTask git-related and metadata fields."""

    def setUp(self):
        self.user = User.objects.create_user(username='gitfielduser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='GitField Project', slug='gitfield-project',
            framework='html',
        )

    def test_linked_branch_and_commit(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='deploy_preview', prompt='Deploy preview',
            linked_branch='feature/login',
            linked_commit_sha='a1b2c3d4e5f6',
        )
        self.assertEqual(task.linked_branch, 'feature/login')
        self.assertEqual(task.linked_commit_sha, 'a1b2c3d4e5f6')

    def test_metadata_json(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='generate_site', prompt='Generate',
            metadata_json={'files_changed': 5, 'duration_ms': 1200},
        )
        self.assertEqual(task.metadata_json['files_changed'], 5)

    def test_system_summary_field(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='Plan',
            system_summary='Found 3 HTML files and 2 CSS files.',
        )
        self.assertEqual(task.system_summary, 'Found 3 HTML files and 2 CSS files.')

    def test_session_key_field(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Edit',
            session_key='sess-xyz-789',
        )
        self.assertEqual(task.session_key, 'sess-xyz-789')

    def test_log_object_key(self):
        task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Edit',
            log_object_key='logs/task-123.txt',
        )
        self.assertEqual(task.log_object_key, 'logs/task-123.txt')

    def test_ordering_newest_first(self):
        t1 = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='plan', prompt='First',
        )
        t2 = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Second',
        )
        tasks = list(AgentTask.objects.all())
        self.assertEqual(tasks[0], t2)
        self.assertEqual(tasks[1], t1)


class AgentTaskAttachmentTests(TestCase):
    """Tests for AgentTaskAttachment model."""

    def setUp(self):
        self.user = User.objects.create_user(username='attachuser', password='pass')
        self.project = Project.objects.create(
            owner=self.user, name='Attach Project', slug='attach-project',
            framework='html',
        )
        self.task = AgentTask.objects.create(
            project=self.project, requested_by=self.user,
            task_type='edit_code', prompt='Add feature',
        )

    def test_create_attachment(self):
        att = AgentTaskAttachment.objects.create(
            task=self.task,
            original_name='screenshot.png',
            file_path='/uploads/task-42/screenshot.png',
            mime_type='image/png',
        )
        self.assertEqual(att.original_name, 'screenshot.png')
        self.assertEqual(att.mime_type, 'image/png')
        self.assertEqual(str(att), f'{self.task.id}:screenshot.png')

    def test_blank_mime_type(self):
        att = AgentTaskAttachment.objects.create(
            task=self.task,
            original_name='data.txt',
            file_path='/uploads/data.txt',
        )
        self.assertEqual(att.mime_type, '')

    def test_task_reverse_relation(self):
        AgentTaskAttachment.objects.create(
            task=self.task, original_name='a.png',
            file_path='/a.png',
        )
        AgentTaskAttachment.objects.create(
            task=self.task, original_name='b.png',
            file_path='/b.png',
        )
        self.assertEqual(self.task.attachments.count(), 2)

    def test_attachment_ordering(self):
        a1 = AgentTaskAttachment.objects.create(
            task=self.task, original_name='z.png', file_path='/z.png',
        )
        a2 = AgentTaskAttachment.objects.create(
            task=self.task, original_name='a.png', file_path='/a.png',
        )
        attachments = list(self.task.attachments.all())
        self.assertEqual(attachments[0], a1)  # first created first


class AgentTaskViewTests(TestCase):
    """Tests for agent views (currently placeholder)."""

    def test_views_module_exists(self):
        from saasclaw_engine.agents import views
        self.assertTrue(hasattr(views, 'render'))

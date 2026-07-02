from celery import shared_task

from saasclaw_engine.deployments.service import deploy_preview, deploy_production


@shared_task(bind=True, queue="deploy")
def run_preview_deploy_job(self, project_id: int, user_id: int | None = None) -> int:
    import logging, traceback
    logger = logging.getLogger(__name__)
    from django.contrib.auth import get_user_model
    from saasclaw_engine.projects.models import Project

    User = get_user_model()
    project = Project.objects.get(id=project_id)
    user = User.objects.get(id=user_id) if user_id else None
    try:
        deployment = deploy_preview(project, triggered_by=user)
        # Queue async screenshot after successful deploy
        take_screenshot.delay(project.slug)
        return deployment.id
    except Exception as exc:
        logger.error("Deploy task failed:\n%s", traceback.format_exc())
        raise


@shared_task(bind=True)
def run_agent_subtask(self, agent_task_id: int | str, model_override: str | None = None) -> str:
    """Run an agent task in the background via Celery.

    Creates a session, runs the agent loop, and updates the task status.
    """
    import logging, traceback
    logger = logging.getLogger(__name__)
    import django
    django.setup()

    from saasclaw_engine.agents.models import AgentTask
    from saasclaw_engine.studio_models.models import Workspace, AgentSession, AgentMessage
    from saasclaw_engine.agent.runner import run_agent

    try:
        task = AgentTask.objects.get(id=agent_task_id)
    except AgentTask.DoesNotExist:
        logger.error("AgentTask %s not found", agent_task_id)
        return f"error: task {agent_task_id} not found"

    task.status = AgentTask.Status.RUNNING
    task.started_at = django.utils.timezone.now()
    task.save(update_fields=["status", "started_at"])

    project = task.project
    try:
        # Get or create a workspace for this project
        ws = Workspace.objects.filter(project=project, is_active=True).first()
        if not ws:
            return f"error: no active workspace for project {project.slug}"

        workspace_path = ws.local_path

        # Create a session for this subtask
        session = AgentSession.objects.create(
            project=project,
            workspace=ws,
            user=task.requested_by,
            title=f"Subtask: {task.prompt[:80]}",
            status="running",
        )
        task.session_key = str(session.id)
        task.save(update_fields=["session_key"])

        # Run the agent
        messages = run_agent(
            workspace_path=workspace_path,
            project_name=project.name,
            conversation=[],
            user_message=task.prompt,
            model=model_override,
            project_id=project.id,
            session_id=str(session.id),
        )

        # Extract final result
        final_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                final_content = msg["content"]
                break

        task.status = AgentTask.Status.SUCCEEDED
        task.result_summary = final_content[:1000]
        task.finished_at = django.utils.timezone.now()
        task.save(update_fields=["status", "result_summary", "finished_at"])

        session.status = "ended"
        session.completed_at = django.utils.timezone.now()
        session.save(update_fields=["status", "completed_at"])

        return str(task.id)

    except Exception as exc:
        logger.error("Agent subtask %s failed: %s", agent_task_id, traceback.format_exc())
        task.status = AgentTask.Status.FAILED
        task.error_message = str(exc)[:1000]
        task.finished_at = django.utils.timezone.now()
        task.save(update_fields=["status", "error_message", "finished_at"])
        raise


@shared_task(bind=True)
def run_production_deploy_job(self, project_id: int, user_id: int | None = None) -> int:
    from django.contrib.auth import get_user_model
    from saasclaw_engine.projects.models import Project

    User = get_user_model()
    project = Project.objects.get(id=project_id)
    user = User.objects.get(id=user_id) if user_id else None
    deployment = deploy_production(project, triggered_by=user)
    return deployment.id


@shared_task
def cleanup_stale_sessions():
    """Auto-end agent sessions idle for more than 15 minutes."""
    import django
    django.setup()
    from django.utils import timezone
    from datetime import timedelta
    from saasclaw_engine.studio_models.models import AgentSession

    cutoff = timezone.now() - timedelta(minutes=15)
    ended = AgentSession.objects.filter(
        status__in=['running', 'idle'],
        updated_at__lt=cutoff,
    ).update(status='ended', updated_at=timezone.now())
    return ended


@shared_task(queue='deploy', acks_late=True, max_retries=1)
def take_screenshot(slug: str) -> bool:
    """Take a screenshot of the preview URL and save to the project directory.

    Runs asynchronously after deploy so it doesn't block anything.
    Uses Playwright Chromium headless.
    """
    import logging, traceback
    logger = logging.getLogger(__name__)
    from pathlib import Path
    from saasclaw_engine.projects.models import Project

    try:
        project = Project.objects.get(slug=slug)
        preview_env = project.environments.filter(name='preview').first()
        if not preview_env or not preview_env.domain:
            logger.info('No preview domain for %s, skipping screenshot', slug)
            return False

        url = f'https://{preview_env.domain}'
        project_dir = Path(project.workspace_root) if project.workspace_root else None
        if not project_dir or not project_dir.exists():
            project_dir = Path('/srv/saasclaw/projects') / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = project_dir / 'screenshot.png'

        from playwright.sync_api import sync_playwright
        import time
        time.sleep(3)  # let the app settle

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 720})
            page.goto(url, wait_until='networkidle', timeout=15000)
            page.screenshot(path=str(screenshot_path), full_page=False)
            browser.close()

        if screenshot_path.exists() and screenshot_path.stat().st_size > 1000:
            logger.info('Screenshot saved for %s (%d bytes)', slug, screenshot_path.stat().st_size)
            return True
        else:
            logger.warning('Screenshot for %s was too small or missing', slug)
            return False
    except Exception:
        logger.warning('Screenshot failed for %s:\n%s', slug, traceback.format_exc())
        return False

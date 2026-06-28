from celery import shared_task

from saasclaw_engine.deployments.service import deploy_preview, deploy_production


@shared_task(bind=True)
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
        return deployment.id
    except Exception as exc:
        logger.error("Deploy task failed:\n%s", traceback.format_exc())
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

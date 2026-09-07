# Ensure the Celery app is loaded when Django starts, so shared_task can find
# it. Added with the social_media_analytics feature — see core/celery.py.
from .celery import app as celery_app

__all__ = ("celery_app",)

"""Celery application for Worldstories.

The project had no Celery setup before the ``social_media_analytics`` app
was added; per that feature's spec, Celery is configured here on the
*existing* project rather than as a parallel app, so any future background
work in Worldstories shares this one worker/broker.

Run locally::

    celery -A core worker -l info
    celery -A core beat -l info

Both need ``DJANGO_SETTINGS_MODULE`` to resolve the same way ``manage.py``
does (``core.settings``, which is ``core/settings/__init__.py`` ->
``env.py``).
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("worldstories")

# All Celery settings live in Django settings under a CELERY_ prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Picks up tasks.py in every app in INSTALLED_APPS.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover - smoke test helper
    print(f"Request: {self.request!r}")

"""Celery tasks for the social-media analytics sync.

Task names are explicit (``social_media_analytics.<name>``) so the Beat
schedule in ``core/settings/base.py`` doesn't depend on the module path, and
so they read clearly next to any future Worldstories tasks on the same
worker.

Every task is:

* **idempotent** — re-running writes another snapshot row, never mutates an
  existing one, so a manual re-run after a failure is always safe;
* **independently retryable** — one platform's failure is recorded on its own
  ``SyncRun`` and never raises into the others (``services.sync_platform``
  swallows per-connection errors by design, spec 3.5).
"""

from __future__ import annotations

import logging

from celery import shared_task

from . import services
from .models import Platform, PlatformConnection, SyncRun

logger = logging.getLogger(__name__)


@shared_task(name="social_media_analytics.sync_meta")
def sync_meta() -> dict:
    """Sync Facebook Pages and Instagram accounts.

    Both run in one task because they share a Meta OAuth grant and a rate
    limit — running them together keeps the app's Graph usage in one bucket
    rather than two competing schedules.
    """

    results = {}
    for platform in (Platform.FACEBOOK, Platform.INSTAGRAM):
        run = services.sync_platform(platform)
        results[platform] = _summarise(run)
    return results


@shared_task(name="social_media_analytics.sync_youtube")
def sync_youtube() -> dict:
    run = services.sync_platform(Platform.YOUTUBE)
    return {Platform.YOUTUBE: _summarise(run)}


@shared_task(name="social_media_analytics.sync_tiktok")
def sync_tiktok() -> dict:
    run = services.sync_platform(Platform.TIKTOK)
    return {Platform.TIKTOK: _summarise(run)}


@shared_task(name="social_media_analytics.sync_all")
def sync_all() -> dict:
    """Convenience entry point for a manual "sync everything now"."""
    out: dict = {}
    for task in (sync_meta, sync_youtube, sync_tiktok):
        try:
            out.update(task())
        except Exception:  # noqa: BLE001 - keep going across platforms
            logger.exception("Sync task %s failed outright", task.name)
    return out


@shared_task(name="social_media_analytics.refresh_expiring_tokens")
def refresh_expiring_tokens() -> dict:
    """Refresh credentials that expire inside the refresh window.

    Runs more often than the syncs because TikTok access tokens live only 24
    hours, while Meta's long-lived user token needs a touch every 60 days.
    """

    refreshed, skipped = 0, 0
    for connection in PlatformConnection.objects.filter(is_active=True):
        if connection.is_token_expired or connection.expires_within(
            services.TOKEN_REFRESH_WINDOW
        ):
            if services.refresh_connection_token(connection):
                refreshed += 1
            else:
                skipped += 1
    return {"refreshed": refreshed, "failed_or_skipped": skipped}


def _summarise(run: SyncRun) -> dict:
    return {
        "status": run.status,
        "items_synced": run.items_synced,
        "snapshots_written": run.snapshots_written,
        "message": run.message,
    }

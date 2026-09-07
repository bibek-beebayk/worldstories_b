"""Business logic shared by the API views and the Celery tasks.

Keeping OAuth completion and snapshot writing here (rather than in views or
tasks) means the connect flow and the scheduled sync exercise exactly the
same code paths, and both are unit-testable without HTTP or a broker.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import integrations
from .integrations.base import IntegrationError, OAuthTokens, TokenExpiredError
from .models import (
    AccountSnapshot,
    ContentItem,
    MetricSnapshot,
    OAuthState,
    Platform,
    PlatformConnection,
    SyncRun,
)

logger = logging.getLogger(__name__)

# Meta long-lived tokens last ~60 days; refresh once inside this window.
TOKEN_REFRESH_WINDOW = timedelta(days=10)


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def build_authorize_url(user, platform: str) -> str:
    """Mint a CSRF state row and return the provider's consent URL.

    The Flutter app opens this URL; the provider redirects back to the
    backend's own callback, so no platform secret or code ever reaches the
    device (spec 2).
    """

    if platform not in Platform.values:
        raise IntegrationError(f"Unknown platform: {platform!r}")

    provider = integrations.PROVIDER_FOR_PLATFORM[platform]
    module = integrations.get_provider_module(provider)

    state = secrets.token_urlsafe(32)
    code_verifier = ""
    kwargs: dict = {}

    if provider == "tiktok":
        # TikTok requires PKCE for web clients. The verifier stays server-side
        # on the state row; only the challenge goes out.
        code_verifier = secrets.token_urlsafe(64)[:128]
        digest = hashlib.sha256(code_verifier.encode()).digest()
        kwargs["code_challenge"] = (
            base64.urlsafe_b64encode(digest).decode().rstrip("=")
        )

    OAuthState.objects.create(
        state=state, user=user, platform=platform, code_verifier=code_verifier
    )
    return module.authorize_url(state, **kwargs)


def complete_oauth(state_value: str, code: str) -> list[PlatformConnection]:
    """Handle a provider redirect: validate state, exchange, persist.

    Returns the connections written — one for most providers, up to two for
    Meta (Page + linked Instagram account).
    """

    try:
        state = OAuthState.objects.select_related("user").get(state=state_value)
    except OAuthState.DoesNotExist as exc:
        raise IntegrationError("Unknown or already-consumed OAuth state") from exc
    if state.used_at is not None:
        raise IntegrationError("This OAuth state has already been used")
    if state.is_expired:
        raise IntegrationError("This OAuth request expired — start the connect again")

    provider = integrations.PROVIDER_FOR_PLATFORM[state.platform]
    module = integrations.get_provider_module(provider)

    if provider == "tiktok":
        bundles = module.exchange_code(code, code_verifier=state.code_verifier)
    elif provider == "youtube":
        bundles = module.exchange_code(code, state=state_value)
    else:
        bundles = module.exchange_code(code)

    connections: list[PlatformConnection] = []
    with transaction.atomic():
        for bundle in bundles:
            platform = bundle.extra.get("platform") or state.platform
            connections.append(_upsert_connection(state.user, platform, bundle))
        state.used_at = timezone.now()
        state.save(update_fields=["used_at"])

    # One-off housekeeping so abandoned connect attempts don't accumulate.
    OAuthState.objects.filter(
        created_at__lt=timezone.now() - timedelta(minutes=OAuthState.STATE_TTL_MINUTES)
    ).delete()

    return connections


def _upsert_connection(user, platform: str, bundle: OAuthTokens) -> PlatformConnection:
    connection, _created = PlatformConnection.objects.update_or_create(
        user=user,
        platform=platform,
        external_account_id=bundle.external_account_id,
        defaults={
            "display_name": bundle.display_name,
            "access_token": bundle.access_token,
            "refresh_token": bundle.refresh_token,
            "token_expires_at": bundle.expires_at,
            "is_active": True,
            "connected_at": timezone.now(),
            "last_sync_status": "",
            "last_sync_error": "",
            "extra": {k: v for k, v in (bundle.extra or {}).items() if k != "platform"},
        },
    )
    return connection


def refresh_connection_token(connection: PlatformConnection) -> bool:
    """Proactively refresh a credential that is close to expiring.

    Returns ``True`` if a refresh actually happened. Never raises for an
    ordinary refresh failure — the connection is marked inactive instead, so
    one dead platform doesn't take a whole sync run down (spec 3.5).
    """

    module = integrations.get_integration(connection.platform)
    refresh = getattr(module, "refresh", None)
    if refresh is None:
        return False

    try:
        bundle = refresh(connection)
    except TokenExpiredError:
        logger.warning(
            "Credential for %s (%s) is expired and cannot be refreshed; "
            "a manual reconnect is required.",
            connection.platform,
            connection.external_account_id,
        )
        connection.is_active = False
        connection.last_sync_status = SyncRun.Status.FAILED
        connection.last_sync_error = "Token expired — reconnect required"
        connection.save(
            update_fields=["is_active", "last_sync_status", "last_sync_error"]
        )
        return False
    except IntegrationError as exc:
        logger.warning("Token refresh failed for %s: %s", connection.platform, exc)
        return False

    connection.access_token = bundle.access_token
    if bundle.refresh_token:
        # TikTok rotates refresh tokens; keeping the old one would break the
        # connection at the next refresh.
        connection.refresh_token = bundle.refresh_token
    connection.token_expires_at = bundle.expires_at
    connection.save(
        update_fields=["access_token", "refresh_token", "token_expires_at"]
    )
    return True


def ensure_fresh_token(connection: PlatformConnection) -> None:
    """Refresh if the token is expired or expiring inside the window."""
    if connection.is_token_expired or connection.expires_within(TOKEN_REFRESH_WINDOW):
        refresh_connection_token(connection)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def sync_connection(connection: PlatformConnection) -> tuple[int, int]:
    """Pull one connection and append snapshots.

    Idempotent in the sense that re-running it never mutates prior rows:
    ``ContentItem`` is upserted on ``(connection, platform_content_id)`` and
    every run *appends* a fresh ``MetricSnapshot``/``AccountSnapshot``
    (spec 3.5 — never overwrite prior snapshots).

    Returns ``(items_synced, snapshots_written)``.
    """

    module = integrations.get_integration(connection.platform)
    ensure_fresh_token(connection)
    captured_at = timezone.now()

    account = module.fetch_account(connection)
    contents = module.fetch_content(connection)

    snapshots_written = 0
    with transaction.atomic():
        AccountSnapshot.objects.create(
            platform_connection=connection,
            captured_at=captured_at,
            follower_count=account.follower_count,
            total_views_lifetime=account.total_views_lifetime,
        )
        snapshots_written += 1

        if account.display_name and account.display_name != connection.display_name:
            connection.display_name = account.display_name

        metric_rows = []
        for fetched in contents:
            if not fetched.platform_content_id:
                continue
            item, _ = ContentItem.objects.update_or_create(
                platform_connection=connection,
                platform_content_id=fetched.platform_content_id,
                defaults={
                    "content_type": fetched.content_type,
                    "caption_or_title": fetched.caption_or_title,
                    "permalink": fetched.permalink,
                    "thumbnail_url": fetched.thumbnail_url,
                    "published_at": fetched.published_at,
                },
            )
            m = fetched.metrics
            metric_rows.append(
                MetricSnapshot(
                    content_item=item,
                    captured_at=captured_at,
                    views=m.views,
                    likes=m.likes,
                    comments=m.comments,
                    shares=m.shares,
                    saves=m.saves,
                    impressions=m.impressions,
                    reach=m.reach,
                )
            )
        MetricSnapshot.objects.bulk_create(metric_rows, batch_size=500)
        snapshots_written += len(metric_rows)

        connection.last_synced_at = captured_at
        connection.last_sync_status = SyncRun.Status.SUCCESS
        connection.last_sync_error = ""
        connection.save(
            update_fields=[
                "display_name",
                "last_synced_at",
                "last_sync_status",
                "last_sync_error",
            ]
        )

    return len(contents), snapshots_written


def sync_platform(platform: str) -> SyncRun:
    """Sync every active connection for one platform, recording a ``SyncRun``.

    A failure on one connection is logged and recorded but does not abort the
    others, and never propagates out of this function — the per-platform
    Celery tasks are independent by construction (spec 3.5).
    """

    run = SyncRun.objects.create(platform=platform, status=SyncRun.Status.SUCCESS)
    connections = PlatformConnection.objects.filter(platform=platform, is_active=True)

    errors: list[str] = []
    items = snapshots = 0

    if not connections.exists():
        run.status = SyncRun.Status.SUCCESS
        run.message = "No active connections for this platform."
        run.finished_at = timezone.now()
        run.save()
        return run

    for connection in connections:
        try:
            got_items, got_snaps = sync_connection(connection)
            items += got_items
            snapshots += got_snaps
        except Exception as exc:  # noqa: BLE001 - one platform must not sink the rest
            message = f"{exc.__class__.__name__}: {exc}"
            logger.exception("Sync failed for %s connection %s", platform, connection.pk)
            errors.append(f"{connection.external_account_id}: {message}")
            connection.last_sync_status = SyncRun.Status.FAILED
            connection.last_sync_error = message[:2000]
            connection.save(update_fields=["last_sync_status", "last_sync_error"])

    run.items_synced = items
    run.snapshots_written = snapshots
    run.finished_at = timezone.now()
    if errors and (items or snapshots):
        run.status = SyncRun.Status.PARTIAL
    elif errors:
        run.status = SyncRun.Status.FAILED
    run.message = "\n".join(errors)[:4000]
    run.save()
    return run

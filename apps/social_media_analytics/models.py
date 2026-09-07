"""Models for the personal social-media analytics feature.

Scope note: this app is deliberately separate from ``apps.stats`` (the
project's existing on-site analytics app, which owns ``/api/analytics/`` and
``/api/admin/analytics/``). Nothing here touches those models or routes.

This is a single-user personal tool, so ``PlatformConnection`` is scoped to a
``User`` only so the admin can tell whose connection it is and so the API can
filter by ``request.user``; there is no multi-tenancy beyond that.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

# Reused from the existing project rather than redefined for this feature.
from core.libs.models import TimeStampModel

from .fields import EncryptedTextField


class Platform(models.TextChoices):
    FACEBOOK = "facebook", "Facebook Page"
    INSTAGRAM = "instagram", "Instagram"
    TIKTOK = "tiktok", "TikTok"
    YOUTUBE = "youtube", "YouTube"


class ContentType(models.TextChoices):
    POST = "post", "Post"
    REEL = "reel", "Reel"
    VIDEO = "video", "Video"
    STORY = "story", "Story"


class PlatformConnection(models.Model):
    """One authorised account on one platform.

    Facebook and Instagram are two rows even though they share a single Meta
    OAuth app and a single user access token: the Page and the IG Business
    account are different external accounts with different metric sets, and
    keeping them separate lets the dashboard treat them as four independent
    platforms exactly as the UI presents them.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="social_platform_connections",
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)

    external_account_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True, default="")

    # Encrypted at rest — see fields.EncryptedTextField. Never log these.
    access_token = EncryptedTextField(blank=True, default="")
    refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)

    connected_at = models.DateTimeField(default=timezone.now)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Populated by the sync tasks so the connections screen can show why a
    # platform is stale without the client having to guess.
    last_sync_status = models.CharField(max_length=20, blank=True, default="")
    last_sync_error = models.TextField(blank=True, default="")

    # Meta only: the IG Business account and the Page it is linked to are
    # discovered during OAuth and cached here so sync doesn't re-walk the
    # graph every run. Null for YouTube/TikTok.
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "platform connection"
        verbose_name_plural = "platform connections"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "platform", "external_account_id"],
                name="uniq_social_connection_per_account",
            )
        ]
        indexes = [models.Index(fields=["user", "platform"])]

    def __str__(self) -> str:
        return f"{self.get_platform_display()}: {self.display_name or self.external_account_id}"

    @property
    def is_token_expired(self) -> bool:
        if self.token_expires_at is None:
            return False
        return self.token_expires_at <= timezone.now()

    def expires_within(self, delta) -> bool:
        """True when the stored token expires inside ``delta`` from now."""
        if self.token_expires_at is None:
            return False
        return self.token_expires_at <= timezone.now() + delta


class ContentItem(TimeStampModel):
    """A single post / reel / video / story on one connected account.

    ``created_at`` (from ``TimeStampModel``) is when we first ingested the
    item, which is not the same as ``published_at``.
    """

    platform_connection = models.ForeignKey(
        PlatformConnection, on_delete=models.CASCADE, related_name="content_items"
    )
    platform_content_id = models.CharField(max_length=255)
    content_type = models.CharField(
        max_length=20, choices=ContentType.choices, default=ContentType.POST
    )

    caption_or_title = models.TextField(blank=True, default="")
    permalink = models.URLField(max_length=1000, blank=True, default="")
    thumbnail_url = models.URLField(max_length=1000, blank=True, default="")

    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["platform_connection", "platform_content_id"],
                name="uniq_social_content_per_connection",
            )
        ]
        indexes = [
            models.Index(fields=["platform_connection", "-published_at"]),
            models.Index(fields=["-published_at"]),
        ]
        ordering = ["-published_at", "-id"]

    def __str__(self) -> str:
        title = (self.caption_or_title or "").strip().splitlines()
        head = title[0][:60] if title else self.platform_content_id
        return f"[{self.platform_connection.platform}] {head}"

    @property
    def platform(self) -> str:
        return self.platform_connection.platform


class MetricSnapshot(models.Model):
    """One row per content item per sync run. Never updated in place.

    Field availability by platform (nulls are meaningful, not missing data):

    ==============  ========  =========  ========  =========
    field           facebook  instagram  tiktok    youtube
    ==============  ========  =========  ========  =========
    views           yes       yes        yes       yes
    likes           yes       yes        yes       yes
    comments        yes       yes        yes       yes
    shares          yes       null       yes       null
    saves           null      yes        null      null
    impressions     yes       yes(*)     null      null
    reach           yes       yes        null      null
    ==============  ========  =========  ========  =========

    (*) Meta retired ``impressions`` for media created after 2024-04-21 in
    Graph API v22+; ``views`` is the replacement. We store whichever the API
    returns and leave the other null.
    """

    content_item = models.ForeignKey(
        ContentItem, on_delete=models.CASCADE, related_name="snapshots"
    )
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    views = models.BigIntegerField(null=True, blank=True)
    likes = models.BigIntegerField(null=True, blank=True)
    comments = models.BigIntegerField(null=True, blank=True)
    shares = models.BigIntegerField(null=True, blank=True)
    saves = models.BigIntegerField(null=True, blank=True)
    impressions = models.BigIntegerField(null=True, blank=True)
    reach = models.BigIntegerField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["content_item", "-captured_at"])]
        ordering = ["-captured_at"]
        get_latest_by = "captured_at"

    def __str__(self) -> str:
        return f"{self.content_item_id} @ {self.captured_at:%Y-%m-%d %H:%M}"


class AccountSnapshot(models.Model):
    """Account-level time series, one row per connection per sync run.

    ``total_views_lifetime`` is null on Facebook Pages and Instagram (no
    lifetime-views metric is exposed); it is available on YouTube
    (``channel.statistics.viewCount``) and absent on TikTok's Display API.
    """

    platform_connection = models.ForeignKey(
        PlatformConnection, on_delete=models.CASCADE, related_name="account_snapshots"
    )
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    follower_count = models.BigIntegerField(null=True, blank=True)
    total_views_lifetime = models.BigIntegerField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["platform_connection", "-captured_at"])]
        ordering = ["-captured_at"]
        get_latest_by = "captured_at"

    def __str__(self) -> str:
        return f"{self.platform_connection_id} @ {self.captured_at:%Y-%m-%d %H:%M}"


class OAuthState(models.Model):
    """Short-lived CSRF state for an in-flight OAuth authorisation.

    The Flutter app asks the backend for an authorise URL, opens it, and the
    provider redirects back to *the backend*. The app never sees the code, so
    the state row is how the callback knows which user/platform it belongs to.
    """

    STATE_TTL_MINUTES = 15

    state = models.CharField(max_length=128, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="social_oauth_states",
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)
    code_verifier = models.CharField(max_length=256, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.platform} state {self.state[:8]}…"

    @property
    def is_expired(self) -> bool:
        return self.created_at < timezone.now() - timezone.timedelta(
            minutes=self.STATE_TTL_MINUTES
        )


class SyncRun(models.Model):
    """Audit row per platform per scheduled sync, surfaced via the API."""

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    platform = models.CharField(max_length=20, choices=Platform.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    items_synced = models.IntegerField(default=0)
    snapshots_written = models.IntegerField(default=0)
    message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["platform", "-started_at"])]

    def __str__(self) -> str:
        return f"{self.platform} {self.status} @ {self.started_at:%Y-%m-%d %H:%M}"

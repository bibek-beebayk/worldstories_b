"""Django admin registrations for manual inspection and debugging.

Token fields are deliberately **never** shown, not even masked-and-readonly:
the values decrypt transparently on attribute access, so putting them on a
ModelAdmin would render live platform credentials into an HTML page. The
admin shows only whether a token exists and when it expires.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils import timezone

from core.libs.admin import ReadOnlyModelAdmin  # reused from the project

from .models import (
    AccountSnapshot,
    ContentItem,
    MetricSnapshot,
    OAuthState,
    PlatformConnection,
    SyncRun,
)


@admin.register(PlatformConnection)
class PlatformConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "platform",
        "display_name",
        "external_account_id",
        "is_active",
        "has_token",
        "token_expires_at",
        "last_synced_at",
        "last_sync_status",
    )
    list_filter = ("platform", "is_active", "last_sync_status")
    search_fields = ("display_name", "external_account_id")
    readonly_fields = (
        "connected_at",
        "last_synced_at",
        "last_sync_status",
        "last_sync_error",
        "has_token",
        "token_state",
    )
    # access_token / refresh_token are excluded, not readonly — see docstring.
    exclude = ("access_token", "refresh_token")
    actions = ("sync_now",)

    @admin.display(boolean=True, description="Token stored")
    def has_token(self, obj: PlatformConnection) -> bool:
        return bool(obj.access_token)

    @admin.display(description="Token state")
    def token_state(self, obj: PlatformConnection) -> str:
        if not obj.access_token:
            return "none"
        if obj.token_expires_at is None:
            return "stored (no expiry reported)"
        if obj.is_token_expired:
            return f"EXPIRED {obj.token_expires_at:%Y-%m-%d %H:%M}"
        remaining = obj.token_expires_at - timezone.now()
        return f"valid for {remaining.days}d"

    @admin.action(description="Sync selected connections now")
    def sync_now(self, request, queryset):
        from . import services

        ok = failed = 0
        for connection in queryset:
            try:
                services.sync_connection(connection)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - report, don't 500 the admin
                failed += 1
                self.message_user(
                    request, f"{connection}: {exc.__class__.__name__}: {exc}", level="ERROR"
                )
        self.message_user(request, f"Synced {ok} connection(s), {failed} failed.")


class MetricSnapshotInline(admin.TabularInline):
    model = MetricSnapshot
    extra = 0
    can_delete = False
    fields = ("captured_at", "views", "likes", "comments", "shares", "saves", "impressions", "reach")
    readonly_fields = fields
    # A long-lived item can have hundreds of snapshots; show the newest few.
    max_num = 20
    ordering = ("-captured_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ContentItem)
class ContentItemAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "platform",
        "content_type",
        "published_at",
        "latest_views",
        "created_at",
    )
    list_filter = ("platform_connection__platform", "content_type")
    search_fields = ("caption_or_title", "platform_content_id", "permalink")
    date_hierarchy = "published_at"
    inlines = (MetricSnapshotInline,)
    raw_id_fields = ("platform_connection",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("platform_connection")

    @admin.display(description="Platform")
    def platform(self, obj: ContentItem) -> str:
        return obj.platform_connection.get_platform_display()

    @admin.display(description="Latest views")
    def latest_views(self, obj: ContentItem):
        snapshot = obj.snapshots.order_by("-captured_at").first()
        return snapshot.views if snapshot else None


@admin.register(MetricSnapshot)
class MetricSnapshotAdmin(ReadOnlyModelAdmin):
    list_display = (
        "content_item",
        "captured_at",
        "views",
        "likes",
        "comments",
        "shares",
        "saves",
        "impressions",
        "reach",
    )
    list_filter = ("content_item__platform_connection__platform", "captured_at")
    raw_id_fields = ("content_item",)
    date_hierarchy = "captured_at"


@admin.register(AccountSnapshot)
class AccountSnapshotAdmin(ReadOnlyModelAdmin):
    list_display = (
        "platform_connection",
        "captured_at",
        "follower_count",
        "total_views_lifetime",
    )
    list_filter = ("platform_connection__platform", "captured_at")
    raw_id_fields = ("platform_connection",)
    date_hierarchy = "captured_at"


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyModelAdmin):
    list_display = (
        "platform",
        "status",
        "started_at",
        "finished_at",
        "items_synced",
        "snapshots_written",
    )
    list_filter = ("platform", "status")
    date_hierarchy = "started_at"


@admin.register(OAuthState)
class OAuthStateAdmin(ReadOnlyModelAdmin):
    """In-flight connect attempts. Useful when a callback mysteriously fails."""

    list_display = ("platform", "user", "created_at", "used_at", "is_expired")
    list_filter = ("platform",)
    # code_verifier is a PKCE secret for an in-flight grant — not displayed.
    exclude = ("code_verifier",)

    @admin.display(boolean=True, description="Expired")
    def is_expired(self, obj: OAuthState) -> bool:
        return obj.is_expired

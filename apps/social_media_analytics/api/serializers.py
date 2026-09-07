"""DRF serializers for the social-media analytics API.

Every response field is explicitly declared — the Flutter client models
(``app/lib/models/``) are hand-written against these shapes, so an implicit
``fields = "__all__"`` here would silently break the client on the next model
change. Token fields are never serialized anywhere in this module.
"""

from __future__ import annotations

from rest_framework import serializers

from ..models import (
    AccountSnapshot,
    ContentItem,
    MetricSnapshot,
    Platform,
    PlatformConnection,
    SyncRun,
)


class MetricSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MetricSnapshot
        fields = [
            "id",
            "captured_at",
            "views",
            "likes",
            "comments",
            "shares",
            "saves",
            "impressions",
            "reach",
        ]


class AccountSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountSnapshot
        fields = ["id", "captured_at", "follower_count", "total_views_lifetime"]


class PlatformConnectionSerializer(serializers.ModelSerializer):
    """Connection status for the connections screen.

    ``follower_count`` is denormalised from the most recent
    ``AccountSnapshot`` so the screen needs one request, not five.
    """

    platform_display = serializers.CharField(source="get_platform_display", read_only=True)
    connected = serializers.SerializerMethodField()
    follower_count = serializers.SerializerMethodField()

    class Meta:
        model = PlatformConnection
        fields = [
            "id",
            "platform",
            "platform_display",
            "external_account_id",
            "display_name",
            "connected",
            "is_active",
            "connected_at",
            "last_synced_at",
            "last_sync_status",
            "last_sync_error",
            "token_expires_at",
            "follower_count",
        ]

    def get_connected(self, obj: PlatformConnection) -> bool:
        return bool(obj.is_active and obj.access_token)

    def get_follower_count(self, obj: PlatformConnection) -> int | None:
        # ``latest_follower_count`` is annotated by ConnectionViewSet in one
        # query; the fallback only runs when the serializer is used outside
        # that view (e.g. from the admin or a test).
        annotated = getattr(obj, "latest_follower_count", None)
        if annotated is not None:
            return annotated
        latest = obj.account_snapshots.order_by("-captured_at").first()
        return latest.follower_count if latest else None


class PlatformStatusSerializer(serializers.Serializer):
    """A row on the connections screen — one per platform, connected or not.

    The client always renders four rows, so the API returns four entries with
    ``connected: false`` for the ones that have no ``PlatformConnection`` yet
    rather than making the client synthesise placeholders.
    """

    platform = serializers.CharField()
    platform_display = serializers.CharField()
    connected = serializers.BooleanField()
    connection = PlatformConnectionSerializer(allow_null=True)


class ContentItemSerializer(serializers.ModelSerializer):
    """A content row with its most recent metrics flattened in."""

    platform = serializers.CharField(source="platform_connection.platform", read_only=True)
    account_display_name = serializers.CharField(
        source="platform_connection.display_name", read_only=True
    )
    latest_metrics = serializers.SerializerMethodField()

    class Meta:
        model = ContentItem
        fields = [
            "id",
            "platform",
            "account_display_name",
            "platform_content_id",
            "content_type",
            "caption_or_title",
            "permalink",
            "thumbnail_url",
            "published_at",
            "created_at",
            "latest_metrics",
        ]

    def get_metrics_source(self, obj: ContentItem):
        snapshot = getattr(obj, "latest_snapshot", None)
        if snapshot is None:
            snapshot = obj.snapshots.order_by("-captured_at").first()
        return snapshot

    def get_latest_metrics(self, obj: ContentItem) -> dict | None:
        snapshot = self.get_metrics_source(obj)
        if snapshot is None:
            return None
        return MetricSnapshotSerializer(snapshot).data


class ContentHistorySerializer(serializers.Serializer):
    """Full metric time series for one item (content detail chart)."""

    content = ContentItemSerializer()
    history = MetricSnapshotSerializer(many=True)


class PlatformSummarySerializer(serializers.Serializer):
    platform = serializers.CharField()
    platform_display = serializers.CharField()
    connected = serializers.BooleanField()
    follower_count = serializers.IntegerField(allow_null=True)
    follower_delta = serializers.IntegerField(allow_null=True)
    content_count = serializers.IntegerField()
    views_this_period = serializers.IntegerField(allow_null=True)
    views_previous_period = serializers.IntegerField(allow_null=True)
    views_delta = serializers.IntegerField(allow_null=True)
    engagement_this_period = serializers.IntegerField(allow_null=True)
    last_synced_at = serializers.DateTimeField(allow_null=True)


class DashboardSummarySerializer(serializers.Serializer):
    period_days = serializers.IntegerField()
    period_start = serializers.DateTimeField()
    previous_period_start = serializers.DateTimeField()
    totals = serializers.DictField()
    platforms = PlatformSummarySerializer(many=True)


class AccountTrendPointSerializer(serializers.Serializer):
    captured_at = serializers.DateTimeField()
    follower_count = serializers.IntegerField(allow_null=True)
    total_views_lifetime = serializers.IntegerField(allow_null=True)


class AccountTrendSerializer(serializers.Serializer):
    platform = serializers.CharField()
    platform_display = serializers.CharField()
    display_name = serializers.CharField()
    points = AccountTrendPointSerializer(many=True)


class SyncRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncRun
        fields = [
            "id",
            "platform",
            "started_at",
            "finished_at",
            "status",
            "items_synced",
            "snapshots_written",
            "message",
        ]


class OAuthUrlSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=Platform.choices)
    authorize_url = serializers.URLField()

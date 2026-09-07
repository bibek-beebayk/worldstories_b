"""DRF views for the social-media analytics API.

Auth: the project already standardises on ``djangorestframework-simplejwt``
(``core/settings/base.py``: ``DEFAULT_AUTHENTICATION_CLASSES``), so this app
reuses it rather than introducing a second scheme. The project-wide default
permission is ``AllowAny``, which is right for Worldstories' public catalogue
and wrong here — every view below sets ``IsAuthenticated`` explicitly, and
every queryset is filtered by ``request.user``.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from django.db.models import F, OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.libs.pagination import PageNumberPagination  # reused from the project

from ..integrations.base import IntegrationError
from ..models import (
    AccountSnapshot,
    ContentItem,
    MetricSnapshot,
    Platform,
    PlatformConnection,
    SyncRun,
)
from .. import services
from .serializers import (
    AccountTrendSerializer,
    ContentItemSerializer,
    DashboardSummarySerializer,
    MetricSnapshotSerializer,
    PlatformConnectionSerializer,
    PlatformStatusSerializer,
    SyncRunSerializer,
)

logger = logging.getLogger(__name__)

DEFAULT_PERIOD_DAYS = 7

# Sort keys the content list accepts, mapped to ORM ordering. Restricting to
# a whitelist keeps an arbitrary ``?sort=`` from becoming an ORM injection
# surface or an accidental unindexed sort.
CONTENT_SORTS = {
    "date": "-published_at",
    "-date": "published_at",
    "views": "-latest_views",
    "-views": "latest_views",
    "likes": "-latest_likes",
    "-likes": "latest_likes",
    "comments": "-latest_comments",
    "-comments": "latest_comments",
}


def _parse_boundary(value: str | None, *, end_of_day: bool = False):
    """Accept either a date (``2025-01-31``) or a full ISO datetime.

    A bare date used as a ``to`` boundary means *the end of* that day, so
    ``?to=2025-01-31`` includes posts published that afternoon.
    """

    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        day = parse_date(value)
        if day is None:
            return None
        moment = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        dt = datetime.combine(day, moment)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class HealthView(APIView):
    """Liveness probe. Public by design — it exposes nothing user-specific."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        return Response(
            {
                "status": "ok",
                "service": "social_media_analytics",
                "time": timezone.now(),
            }
        )


class ConnectionViewSet(viewsets.ViewSet):
    """Platform connection status and the OAuth entry points.

    Routed manually (not via a DRF router) because the detail lookup is a
    *platform slug*, not a pk — the client thinks in platforms, and there is
    at most one meaningful connection per platform for a single user.
    """

    permission_classes = [IsAuthenticated]

    def _connections(self, request):
        """Connections with the newest account snapshot attached in one query."""
        newest = AccountSnapshot.objects.filter(
            platform_connection=OuterRef("pk")
        ).order_by("-captured_at")
        return PlatformConnection.objects.filter(user=request.user).annotate(
            latest_follower_count=Subquery(newest.values("follower_count")[:1])
        )

    def list(self, request):
        """One row per platform, connected or not (spec 3.6)."""
        by_platform: dict[str, PlatformConnection] = {}
        for connection in self._connections(request):
            # If a platform somehow has several rows, prefer the active one
            # and then the most recently synced.
            current = by_platform.get(connection.platform)
            if current is None or _connection_rank(connection) > _connection_rank(current):
                by_platform[connection.platform] = connection

        rows = []
        for platform, label in Platform.choices:
            connection = by_platform.get(platform)
            rows.append(
                {
                    "platform": platform,
                    "platform_display": label,
                    "connected": bool(
                        connection and connection.is_active and connection.access_token
                    ),
                    # The model instance, not pre-serialized data — the
                    # nested PlatformConnectionSerializer does that itself.
                    "connection": connection,
                }
            )
        return Response(PlatformStatusSerializer(rows, many=True).data)

    @action(detail=False, methods=["get"], url_path=r"(?P<platform>[\w-]+)/oauth-url")
    def oauth_url(self, request, platform: str = ""):
        """Return the provider consent URL for the app to open."""
        if platform not in Platform.values:
            return Response(
                {"detail": f"Unknown platform '{platform}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            url = services.build_authorize_url(request.user, platform)
        except IntegrationError as exc:
            # Almost always "credentials not configured" — a 503 tells the
            # client this is a server-side gap, not a bad request.
            return Response(
                {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response({"platform": platform, "authorize_url": url})

    @action(detail=False, methods=["post"], url_path=r"(?P<platform>[\w-]+)/disconnect")
    def disconnect(self, request, platform: str = ""):
        """Deactivate a connection and destroy its stored credentials.

        Historical ``ContentItem``/``MetricSnapshot`` rows are kept —
        disconnecting is not "delete my analytics history", and reconnecting
        the same account later resumes the same series.
        """

        if platform not in Platform.values:
            return Response(
                {"detail": f"Unknown platform '{platform}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        updated = PlatformConnection.objects.filter(
            user=request.user, platform=platform
        ).update(
            is_active=False,
            access_token="",
            refresh_token="",
            token_expires_at=None,
            last_sync_status="",
            last_sync_error="",
        )
        if not updated:
            return Response(
                {"detail": "No connection for that platform."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"platform": platform, "connected": False})

    @action(detail=False, methods=["get"], url_path="sync-status")
    def sync_status(self, request):
        """Most recent ``SyncRun`` per platform (spec 3.5)."""
        rows = []
        for platform, label in Platform.choices:
            run = SyncRun.objects.filter(platform=platform).first()
            rows.append(
                {
                    "platform": platform,
                    "platform_display": label,
                    "last_run": SyncRunSerializer(run).data if run else None,
                }
            )
        return Response(rows)


def _connection_rank(connection: PlatformConnection) -> tuple:
    return (
        1 if connection.is_active else 0,
        connection.last_synced_at.timestamp() if connection.last_synced_at else 0,
    )


class ContentViewSet(viewsets.ViewSet):
    """Content list with latest metrics, plus per-item history."""

    permission_classes = [IsAuthenticated]
    pagination_class = PageNumberPagination

    def get_queryset(self, request):
        """Annotate each item with its newest snapshot's headline metrics.

        Correlated subqueries rather than a join+aggregate: there is one row
        per item per sync, so a naive join would multiply rows by the number
        of sync runs and make sorting by "current views" wrong.
        """

        latest = MetricSnapshot.objects.filter(content_item=OuterRef("pk")).order_by(
            "-captured_at"
        )
        qs = (
            ContentItem.objects.filter(platform_connection__user=request.user)
            .select_related("platform_connection")
            .annotate(
                latest_views=Subquery(latest.values("views")[:1]),
                latest_likes=Subquery(latest.values("likes")[:1]),
                latest_comments=Subquery(latest.values("comments")[:1]),
            )
        )

        platform = request.query_params.get("platform")
        if platform:
            platforms = [p for p in platform.split(",") if p in Platform.values]
            if platforms:
                qs = qs.filter(platform_connection__platform__in=platforms)

        start = _parse_boundary(request.query_params.get("from"))
        end = _parse_boundary(request.query_params.get("to"), end_of_day=True)
        if start:
            qs = qs.filter(published_at__gte=start)
        if end:
            qs = qs.filter(published_at__lte=end)

        search = request.query_params.get("q")
        if search:
            qs = qs.filter(caption_or_title__icontains=search)

        sort = request.query_params.get("sort", "date")
        ordering = CONTENT_SORTS.get(sort, "-published_at")
        # Nulls last regardless of direction: an item we have no snapshot for
        # should never outrank one with real numbers.
        field = ordering.lstrip("-")
        if field == "published_at":
            qs = qs.order_by(ordering, "-id")
        else:
            # Nulls last regardless of direction: an item we have no snapshot
            # for should never outrank one with real numbers.
            key = (
                F(field).desc(nulls_last=True)
                if ordering.startswith("-")
                else F(field).asc(nulls_last=True)
            )
            qs = qs.order_by(key, "-published_at", "-id")
        return qs

    def list(self, request):
        queryset = self.get_queryset(request)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        # The annotations above already carry the headline numbers, but the
        # serializer returns the full snapshot; prefetch it in one query
        # instead of one per row.
        _attach_latest_snapshots(page or [])
        serializer = ContentItemSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def retrieve(self, request, pk=None):
        item = self._get_item(request, pk)
        if item is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        _attach_latest_snapshots([item])
        return Response(ContentItemSerializer(item).data)

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        """Full ``MetricSnapshot`` series for the detail-screen trend chart."""
        item = self._get_item(request, pk)
        if item is None:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        snapshots = item.snapshots.order_by("captured_at")

        start = _parse_boundary(request.query_params.get("from"))
        end = _parse_boundary(request.query_params.get("to"), end_of_day=True)
        if start:
            snapshots = snapshots.filter(captured_at__gte=start)
        if end:
            snapshots = snapshots.filter(captured_at__lte=end)

        _attach_latest_snapshots([item])
        return Response(
            {
                "content": ContentItemSerializer(item).data,
                "history": MetricSnapshotSerializer(snapshots, many=True).data,
            }
        )

    def _get_item(self, request, pk):
        return (
            ContentItem.objects.filter(
                pk=pk, platform_connection__user=request.user
            )
            .select_related("platform_connection")
            .first()
        )


def _attach_latest_snapshots(items) -> None:
    """Bulk-load the newest snapshot per item onto ``latest_snapshot``."""
    items = list(items)
    if not items:
        return
    ids = [item.pk for item in items]
    newest = (
        MetricSnapshot.objects.filter(content_item_id__in=ids)
        .order_by("content_item_id", "-captured_at")
        .distinct("content_item_id")
    )
    by_item = {row.content_item_id: row for row in newest}
    for item in items:
        item.latest_snapshot = by_item.get(item.pk)


class DashboardSummaryView(APIView):
    """Aggregated totals + week-over-week deltas for the home screen.

    "Views this period" is a **delta**, not a sum of cumulative counters:
    every platform here reports lifetime totals per post, so summing the
    latest snapshots would give lifetime views, and comparing two such sums
    across periods would double-count everything that existed in both. We
    instead diff each item's newest snapshot in the period against its newest
    snapshot before the period.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            days = max(1, min(365, int(request.query_params.get("days", DEFAULT_PERIOD_DAYS))))
        except (TypeError, ValueError):
            days = DEFAULT_PERIOD_DAYS

        now = timezone.now()
        period_start = now - timedelta(days=days)
        previous_start = now - timedelta(days=days * 2)

        connections = {
            c.platform: c
            for c in PlatformConnection.objects.filter(user=request.user, is_active=True)
        }

        platforms = []
        totals = {
            "followers": 0,
            "views_this_period": 0,
            "views_previous_period": 0,
            "views_delta": 0,
            "engagement_this_period": 0,
            "content_count": 0,
        }

        for platform, label in Platform.choices:
            connection = connections.get(platform)
            if connection is None:
                platforms.append(
                    {
                        "platform": platform,
                        "platform_display": label,
                        "connected": False,
                        "follower_count": None,
                        "follower_delta": None,
                        "content_count": 0,
                        "views_this_period": None,
                        "views_previous_period": None,
                        "views_delta": None,
                        "engagement_this_period": None,
                        "last_synced_at": None,
                    }
                )
                continue

            followers_now, followers_then = _follower_pair(connection, period_start)
            this_period = _period_delta(connection, period_start, now)
            prev_period = _period_delta(connection, previous_start, period_start)
            content_count = ContentItem.objects.filter(
                platform_connection=connection
            ).count()

            views_now = this_period["views"]
            views_prev = prev_period["views"]
            engagement = this_period["engagement"]

            platforms.append(
                {
                    "platform": platform,
                    "platform_display": label,
                    "connected": True,
                    "follower_count": followers_now,
                    "follower_delta": (
                        followers_now - followers_then
                        if followers_now is not None and followers_then is not None
                        else None
                    ),
                    "content_count": content_count,
                    "views_this_period": views_now,
                    "views_previous_period": views_prev,
                    "views_delta": (
                        views_now - views_prev
                        if views_now is not None and views_prev is not None
                        else None
                    ),
                    "engagement_this_period": engagement,
                    "last_synced_at": connection.last_synced_at,
                }
            )

            totals["followers"] += followers_now or 0
            totals["views_this_period"] += views_now or 0
            totals["views_previous_period"] += views_prev or 0
            totals["engagement_this_period"] += engagement or 0
            totals["content_count"] += content_count

        totals["views_delta"] = (
            totals["views_this_period"] - totals["views_previous_period"]
        )

        payload = {
            "period_days": days,
            "period_start": period_start,
            "previous_period_start": previous_start,
            "totals": totals,
            "platforms": platforms,
        }
        return Response(DashboardSummarySerializer(payload).data)


def _follower_pair(connection: PlatformConnection, boundary) -> tuple[int | None, int | None]:
    """(current followers, followers as of ``boundary``)."""
    latest = connection.account_snapshots.order_by("-captured_at").first()
    earlier = (
        connection.account_snapshots.filter(captured_at__lte=boundary)
        .order_by("-captured_at")
        .first()
    )
    return (
        latest.follower_count if latest else None,
        earlier.follower_count if earlier else None,
    )


def _period_delta(connection: PlatformConnection, start, end) -> dict:
    """Growth in cumulative counters between ``start`` and ``end``.

    For each content item we take the newest snapshot at/before ``end`` and
    subtract the newest snapshot at/before ``start``. Items first seen inside
    the window have no baseline, so their whole count is treated as growth —
    which is correct: those views were all earned in the period.
    """

    items = list(
        ContentItem.objects.filter(platform_connection=connection).values_list(
            "id", flat=True
        )
    )
    if not items:
        return {"views": 0, "engagement": 0}

    end_rows = _snapshot_at(items, end)
    start_rows = _snapshot_at(items, start)

    views = engagement = 0
    for item_id, row in end_rows.items():
        base = start_rows.get(item_id)
        # Facebook Page posts report no "views" — impressions is the closest
        # equivalent, so it stands in here rather than the platform showing a
        # flat zero on the dashboard. Both columns stay distinct in the raw
        # snapshot data; this coalescing is a presentation choice.
        views += _diff(
            _view_like(row), _view_like(base) if base else None
        )
        # "Engagement" = the interaction columns a platform actually reports;
        # nulls contribute nothing rather than being coerced to zero.
        for field in ("likes", "comments", "shares", "saves"):
            engagement += _diff(row.get(field), base.get(field) if base else None)
    return {"views": views, "engagement": engagement}


def _snapshot_at(item_ids: list[int], moment) -> dict[int, dict]:
    """Newest snapshot at/before ``moment`` for each item, keyed by item id."""
    rows = (
        MetricSnapshot.objects.filter(content_item_id__in=item_ids, captured_at__lte=moment)
        .order_by("content_item_id", "-captured_at")
        .distinct("content_item_id")
        .values(
            "content_item_id",
            "views",
            "impressions",
            "likes",
            "comments",
            "shares",
            "saves",
        )
    )
    return {row["content_item_id"]: row for row in rows}


def _view_like(row: dict):
    """A platform's best available "how many people saw this" counter."""
    views = row.get("views")
    return views if views is not None else row.get("impressions")


def _diff(current, base) -> int:
    if current is None:
        return 0
    if base is None:
        return int(current)
    # Counters can go down (deleted comments, unlikes); clamp so a decrease
    # on one post can't read as negative growth for the whole account.
    return max(0, int(current) - int(base))


class AccountTrendsView(APIView):
    """Follower / lifetime-view series per platform for the trends screen."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        platform = request.query_params.get("platform")
        try:
            days = max(1, min(730, int(request.query_params.get("days", 90))))
        except (TypeError, ValueError):
            days = 90
        since = timezone.now() - timedelta(days=days)

        connections = PlatformConnection.objects.filter(user=request.user)
        if platform:
            platforms = [p for p in platform.split(",") if p in Platform.values]
            if platforms:
                connections = connections.filter(platform__in=platforms)

        series = []
        for connection in connections:
            points = (
                connection.account_snapshots.filter(captured_at__gte=since)
                .order_by("captured_at")
                .values("captured_at", "follower_count", "total_views_lifetime")
            )
            series.append(
                {
                    "platform": connection.platform,
                    "platform_display": connection.get_platform_display(),
                    "display_name": connection.display_name,
                    "points": list(points),
                }
            )
        return Response(AccountTrendSerializer(series, many=True).data)


class TriggerSyncView(APIView):
    """Kick a sync by hand from the app (pull-to-refresh's "sync now").

    Enqueues the Celery task when a broker is reachable and falls back to
    running inline, so the feature still works on a dev box with no worker.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from ..tasks import sync_all

        try:
            result = sync_all.delay()
            return Response({"queued": True, "task_id": result.id})
        except Exception as exc:  # noqa: BLE001 - broker down is not a 500 here
            logger.warning("Could not enqueue sync_all (%s); running inline", exc)
            summary = sync_all()
            return Response({"queued": False, "result": summary})

import csv
import logging
import time
from datetime import datetime, time as date_time, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import TruncWeek
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.story.api import IsSuperUser
from apps.story.models import Story

from .admin_serializers import (
    AdminDigestDeliverySerializer,
    AdminDigestIssueSerializer,
    AdminSubscriberSerializer,
    AdminTestSendSerializer,
)
from .digest import build_digest
from .models import DigestDelivery, DigestIssue, NewsletterSubscription, newsletter_token
from .services import (
    build_digest_message,
    build_digest_subject,
    get_week_featured_story,
    prepare_digest_context,
    render_email_template,
    send_confirmation_email,
    send_message_or_raise,
    send_test_digest,
)

logger = logging.getLogger(__name__)

SHORT_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
VALID_STATUSES = {
    NewsletterSubscription.STATUS_PENDING,
    NewsletterSubscription.STATUS_CONFIRMED,
    NewsletterSubscription.STATUS_UNSUBSCRIBED,
}
VALID_SOURCES = {value for value, _label in NewsletterSubscription.SOURCE_CHOICES}
CSV_INJECTION_PREFIXES = ("=", "+", "-", "@")
RETRY_MAX_PER_REQUEST = 80
RETRY_MIN_INTERVAL_SECONDS = 0.2  # max 5 sends/second


def _parse_positive_int(raw: str | None, default: int, maximum: int | None = None) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    if maximum is not None:
        return min(value, maximum)
    return value


def _filter_subscribers(request: Request) -> QuerySet[NewsletterSubscription]:
    qs = NewsletterSubscription.objects.all().order_by("-created_at", "-id")
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(email__icontains=q)
    sub_status = (request.query_params.get("status") or "").strip()
    if sub_status in VALID_STATUSES:
        qs = qs.filter(status=sub_status)
    source = (request.query_params.get("source") or "").strip()
    if source in VALID_SOURCES:
        qs = qs.filter(source=source)
    weekday_raw = (request.query_params.get("weekday") or "").strip()
    if weekday_raw.isdigit():
        weekday = int(weekday_raw)
        if 0 <= weekday <= 6:
            qs = qs.filter(send_weekday=weekday)
    return qs


def _safe_csv_cell(value: object) -> str:
    if value is None:
        return ""
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    if text and text[0] in CSV_INJECTION_PREFIXES:
        return f"'{text}"
    return text


class _EchoBuffer:
    def write(self, value: str) -> str:
        return value


class NewsletterAdminStatsAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request: Request) -> Response:
        tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
        now = timezone.now()
        counts = NewsletterSubscription.objects.aggregate(
            confirmed=Count("id", filter=Q(status=NewsletterSubscription.STATUS_CONFIRMED)),
            pending=Count("id", filter=Q(status=NewsletterSubscription.STATUS_PENDING)),
            unsubscribed=Count("id", filter=Q(status=NewsletterSubscription.STATUS_UNSUBSCRIBED)),
            total=Count("id"),
            new_this_week=Count(
                "id",
                filter=Q(
                    status=NewsletterSubscription.STATUS_CONFIRMED,
                    confirmed_at__gte=now - timedelta(days=7),
                ),
            ),
        )
        total = counts["total"]
        unsubscribe_rate = round(counts["unsubscribed"] / total, 3) if total else 0.0

        local_today = now.astimezone(tz).date()
        current_week_start = local_today - timedelta(days=local_today.weekday())
        week_starts = [current_week_start - timedelta(weeks=offset) for offset in range(11, -1, -1)]
        window_start = datetime.combine(week_starts[0], date_time.min, tzinfo=tz)

        growth_by_week: dict[str, dict[str, object]] = {
            week_start.isoformat(): {
                "week_start": week_start.isoformat(),
                "confirmed": 0,
                "by_source": {},
            }
            for week_start in week_starts
        }
        growth_rows = (
            NewsletterSubscription.objects.filter(
                status=NewsletterSubscription.STATUS_CONFIRMED,
                confirmed_at__gte=window_start,
            )
            .annotate(week=TruncWeek("confirmed_at", tzinfo=tz))
            .values("week", "source")
            .annotate(count=Count("id"))
            .order_by("week", "source")
        )
        for row in growth_rows:
            week_dt = row["week"]
            if week_dt is None:
                continue
            week_key = (
                week_dt.astimezone(tz).date().isoformat()
                if timezone.is_aware(week_dt)
                else week_dt.date().isoformat()
            )
            bucket = growth_by_week.get(week_key)
            if bucket is None:
                continue
            row_count = int(row["count"])
            bucket["confirmed"] = int(bucket["confirmed"]) + row_count
            by_source = bucket["by_source"]
            if isinstance(by_source, dict):
                by_source[row["source"]] = row_count

        weekday_counts = dict(
            NewsletterSubscription.objects.filter(
                status=NewsletterSubscription.STATUS_CONFIRMED,
                send_weekday__isnull=False,
            )
            .values("send_weekday")
            .annotate(count=Count("id"))
            .values_list("send_weekday", "count")
        )
        weekday_load = [
            {
                "weekday": day,
                "label": SHORT_WEEKDAY_LABELS[day],
                "count": weekday_counts.get(day, 0),
            }
            for day in range(7)
        ]

        return Response(
            {
                "confirmed": counts["confirmed"],
                "pending": counts["pending"],
                "unsubscribed": counts["unsubscribed"],
                "total": total,
                "new_this_week": counts["new_this_week"],
                "unsubscribe_rate": unsubscribe_rate,
                "weekly_growth": list(growth_by_week.values()),
                "weekday_load": weekday_load,
                "daily_send_limit": settings.NEWSLETTER_DAILY_SEND_LIMIT,
            }
        )


class NewsletterAdminSubscribersAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request: Request) -> Response:
        qs = _filter_subscribers(request)
        page = _parse_positive_int(request.query_params.get("page"), default=1)
        page_size = _parse_positive_int(request.query_params.get("page_size"), default=25, maximum=100)
        count = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start : start + page_size])
        return Response(
            {
                "count": count,
                "page": page,
                "page_size": page_size,
                "results": AdminSubscriberSerializer(rows, many=True).data,
            }
        )


class NewsletterAdminSubscribersExportAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request: Request) -> StreamingHttpResponse:
        qs = _filter_subscribers(request).values_list(
            "email",
            "status",
            "source",
            "language",
            "send_weekday",
            "created_at",
            "confirmed_at",
        )
        tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
        today_str = timezone.now().astimezone(tz).date().isoformat()

        def row_stream() -> Iterator[str]:
            writer = csv.writer(_EchoBuffer())
            yield writer.writerow(
                [
                    "email",
                    "status",
                    "source",
                    "language",
                    "send_weekday",
                    "created_at",
                    "confirmed_at",
                ]
            )
            for row in qs.iterator(chunk_size=500):
                yield writer.writerow([_safe_csv_cell(cell) for cell in row])

        response = StreamingHttpResponse(row_stream(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="newsletter-subscribers-{today_str}.csv"'
        )
        return response


class NewsletterAdminResendConfirmationAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request: Request, pk: int) -> Response:
        subscription = get_object_or_404(NewsletterSubscription, pk=pk)
        if subscription.status != NewsletterSubscription.STATUS_PENDING:
            return Response(
                {"detail": "Confirmation email can only be resent to pending subscribers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not subscription.confirm_token:
            subscription.confirm_token = newsletter_token()
            subscription.save(update_fields=["confirm_token"])
        send_confirmation_email(subscription)
        subscription.confirmation_sent_at = timezone.now()
        subscription.confirmation_send_count += 1
        subscription.save(update_fields=["confirmation_sent_at", "confirmation_send_count"])
        return Response({"detail": "Confirmation email sent."})


class NewsletterAdminUnsubscribeAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request: Request, pk: int) -> Response:
        subscription = get_object_or_404(NewsletterSubscription, pk=pk)
        if subscription.status not in (
            NewsletterSubscription.STATUS_CONFIRMED,
            NewsletterSubscription.STATUS_PENDING,
        ):
            return Response(
                {"detail": "Subscriber is already unsubscribed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        subscription.status = NewsletterSubscription.STATUS_UNSUBSCRIBED
        subscription.unsubscribed_at = subscription.unsubscribed_at or timezone.now()
        subscription.save(update_fields=["status", "unsubscribed_at"])
        return Response(AdminSubscriberSerializer(subscription).data)


class NewsletterAdminIssuesAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request: Request) -> Response:
        qs = DigestIssue.objects.select_related("featured_story").order_by("-issue_date", "-id")
        page = _parse_positive_int(request.query_params.get("page"), default=1)
        page_size = _parse_positive_int(request.query_params.get("page_size"), default=20, maximum=100)
        count = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start : start + page_size])
        return Response(
            {
                "count": count,
                "page": page,
                "page_size": page_size,
                "results": AdminDigestIssueSerializer(rows, many=True).data,
            }
        )


class NewsletterAdminIssueDeliveriesAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request: Request, pk: int) -> Response:
        issue = get_object_or_404(DigestIssue, pk=pk)
        delivery_status = (request.query_params.get("status") or DigestDelivery.STATUS_FAILED).strip()
        qs = issue.deliveries.select_related("subscription").order_by("id")
        if delivery_status in (DigestDelivery.STATUS_SENT, DigestDelivery.STATUS_FAILED):
            qs = qs.filter(status=delivery_status)
        rows = list(qs[:200])
        return Response({"results": AdminDigestDeliverySerializer(rows, many=True).data})


class NewsletterAdminRetryFailedAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request: Request, pk: int) -> Response:
        issue = get_object_or_404(DigestIssue.objects.select_related("featured_story"), pk=pk)
        tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
        now = timezone.now()
        today_date = now.astimezone(tz).date()
        day_start = datetime.combine(today_date, date_time.min, tzinfo=tz)
        day_end = day_start + timedelta(days=1)

        already_sent_today = DigestDelivery.objects.filter(
            status=DigestDelivery.STATUS_SENT,
            sent_at__gte=day_start,
            sent_at__lt=day_end,
        ).count()
        allowed = min(
            RETRY_MAX_PER_REQUEST,
            max(0, settings.NEWSLETTER_DAILY_SEND_LIMIT - already_sent_today),
        )

        failed_deliveries = list(
            issue.deliveries.select_related("subscription", "subscription__user")
            .filter(
                status=DigestDelivery.STATUS_FAILED,
                subscription__status=NewsletterSubscription.STATUS_CONFIRMED,
            )
            .order_by("id")[:allowed]
        )

        since = now - timedelta(days=7)
        featured_slug = issue.featured_story.slug if issue.featured_story_id else None
        attempted = 0
        sent = 0
        failed = 0
        last_send_ts: float | None = None

        for delivery in failed_deliveries:
            if last_send_ts is not None:
                elapsed = time.monotonic() - last_send_ts
                if elapsed < RETRY_MIN_INTERVAL_SECONDS:
                    time.sleep(RETRY_MIN_INTERVAL_SECONDS - elapsed)
            attempted += 1
            subscription = delivery.subscription
            last_send_ts = time.monotonic()
            try:
                personalized = build_digest(
                    since,
                    subscription=subscription,
                    story_slug=featured_slug,
                    now=now,
                )
                message = build_digest_message(
                    issue.subject,
                    personalized,
                    issue.issue_date,
                    subscription.email,
                    subscription,
                )
                send_message_or_raise(message)
                sent_at = timezone.now()
                delivery.status = DigestDelivery.STATUS_SENT
                delivery.error = ""
                delivery.sent_at = sent_at
                delivery.save(update_fields=["status", "error", "sent_at"])
                subscription.last_digest_sent_at = sent_at
                subscription.save(update_fields=["last_digest_sent_at"])
                sent += 1
            except Exception as exc:
                delivery.status = DigestDelivery.STATUS_FAILED
                delivery.error = str(exc)
                delivery.sent_at = None
                delivery.save(update_fields=["status", "error", "sent_at"])
                failed += 1

        issue.sent_count = issue.deliveries.filter(status=DigestDelivery.STATUS_SENT).count()
        issue.failed_count = issue.deliveries.filter(status=DigestDelivery.STATUS_FAILED).count()
        issue.status = (
            DigestIssue.STATUS_FAILED if issue.failed_count else DigestIssue.STATUS_FINISHED
        )
        issue.finished_at = timezone.now()
        issue.save(update_fields=["sent_count", "failed_count", "status", "finished_at"])

        return Response(
            {
                "attempted": attempted,
                "sent": sent,
                "failed": failed,
                "remaining_failed": issue.failed_count,
            }
        )


class NewsletterAdminTestSendAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request: Request) -> Response:
        serializer = AdminTestSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recipient = (request.user.email or "").strip()
        if not recipient:
            return Response(
                {"detail": "Your account does not have an email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        story_slug = (serializer.validated_data.get("story_slug") or "").strip() or None
        if story_slug and not Story.objects.published().filter(slug=story_slug).exists():
            return Response(
                {"detail": "story_slug must identify a published story."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            send_test_digest(recipient, story_slug=story_slug)
        except Exception as exc:
            logger.exception("Failed to send admin test digest to %s", recipient)
            return Response(
                {"detail": str(exc)[:200] or "Failed to send test digest."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": f"Test digest sent to {recipient}."})


class NewsletterAdminPreviewAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request: Request) -> Response:
        tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
        now = timezone.now()
        issue_date = now.astimezone(tz).date()
        since = now - timedelta(days=7)
        week_story = get_week_featured_story(issue_date)
        story_slug = week_story.slug if week_story else None
        data = build_digest(since, subscription=None, story_slug=story_slug, now=now)
        if not data["new_stories"] and not data["new_posts"]:
            return Response(
                {
                    "subject": None,
                    "html": None,
                    "reason": "No new stories this week",
                }
            )
        subject = build_digest_subject(data)
        context = prepare_digest_context(data, issue_date, subscription=None)
        html = render_email_template("newsletter/digest.html", context)
        return Response({"subject": subject, "html": html})

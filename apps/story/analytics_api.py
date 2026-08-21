from datetime import timedelta

from django.db.models import Avg, Count, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.stats.models import AnalyticsEvent, AudioReadingProgress, ChapterReadingProgress, ReadingProgress
from apps.users.models import User

from .api import IsSuperUser
from .models import Blog, Favorite, Genre, Review, Story, StoryView, Submission, published_blog_q, published_story_q

ALLOWED_RANGE_DAYS = (7, 30, 90, 365)
DEFAULT_RANGE_DAYS = 30
CACHE_SECONDS = 60 * 5


def get_range_days(request):
    try:
        days = int(request.query_params.get("days", DEFAULT_RANGE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RANGE_DAYS
    return days if days in ALLOWED_RANGE_DAYS else DEFAULT_RANGE_DAYS


def get_cutoff(days):
    return timezone.now() - timedelta(days=days)


class AdminAnalyticsContentAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        cutoff = get_cutoff(days)

        views_over_time = (
            StoryView.objects.filter(created_at__gte=cutoff)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )

        genre_qs = (
            Genre.objects.annotate(
                stories_count=Count("stories", filter=published_story_q("stories"), distinct=True),
                avg_rating=Avg("stories__rating", filter=published_story_q("stories")),
                total_views=Sum("stories__views", filter=published_story_q("stories")),
                total_favorites=Count("stories__favorites", filter=published_story_q("stories"), distinct=True),
            )
            .filter(stories_count__gt=0)
            .order_by("-total_views")
        )
        genre_performance = [
            {
                "id": genre.id,
                "name": genre.name,
                "stories_count": genre.stories_count,
                "avg_rating": round(genre.avg_rating or 0, 2),
                "total_views": genre.total_views or 0,
                "total_favorites": genre.total_favorites,
            }
            for genre in genre_qs
        ]

        story_type_breakdown = (
            Story.objects.published()
            .values("story_type")
            .annotate(count=Count("id"), avg_rating=Avg("rating"), avg_views=Avg("views"))
            .order_by("-count")
        )

        completion_split = (
            Story.objects.published()
            .values("is_completed")
            .annotate(count=Count("id"), avg_rating=Avg("rating"), avg_views=Avg("views"))
        )

        publishing_over_time = (
            Story.objects.filter(published_story_q(), site_published_date__gte=cutoff.date())
            .values("site_published_date")
            .annotate(count=Count("id"))
            .order_by("site_published_date")
        )

        # Blog has no site_published_date-style field — its own BlogSerializer
        # already treats created_at as the effective "published at" moment
        # (published_at = source="created_at"), so this mirrors that.
        blog_publishing_over_time = (
            Blog.objects.filter(published_blog_q(), created_at__gte=cutoff)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )
        blog_posts_count = Blog.objects.published().count()

        return Response(
            {
                "range_days": days,
                "views_over_time": [{"day": row["day"], "count": row["count"]} for row in views_over_time],
                "genre_performance": genre_performance,
                "story_type_breakdown": [
                    {
                        "story_type": row["story_type"],
                        "count": row["count"],
                        "avg_rating": round(row["avg_rating"] or 0, 2),
                        "avg_views": round(row["avg_views"] or 0, 1),
                    }
                    for row in story_type_breakdown
                ],
                "completion_split": [
                    {
                        "is_completed": row["is_completed"],
                        "count": row["count"],
                        "avg_rating": round(row["avg_rating"] or 0, 2),
                        "avg_views": round(row["avg_views"] or 0, 1),
                    }
                    for row in completion_split
                ],
                "publishing_over_time": [
                    {"day": row["site_published_date"], "count": row["count"]} for row in publishing_over_time
                ],
                "blog_publishing_over_time": [
                    {"day": row["day"], "count": row["count"]} for row in blog_publishing_over_time
                ],
                "blog_posts_count": blog_posts_count,
            }
        )


class AdminAnalyticsEngagementAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        cutoff = get_cutoff(days)

        progress_qs = ReadingProgress.objects.filter(updated_at__gte=cutoff)
        bucket_defs = [
            ("0-25%", 0.0, 0.25),
            ("25-50%", 0.25, 0.5),
            ("50-75%", 0.5, 0.75),
            ("75-100%", 0.75, 1.01),
        ]
        reading_progress_buckets = [
            {"bucket": label, "count": progress_qs.filter(progress__gte=lo, progress__lt=hi).count()}
            for label, lo, hi in bucket_defs
        ]

        chapter_dropoff = (
            ChapterReadingProgress.objects.filter(updated_at__gte=cutoff)
            .values("chapter__order")
            .annotate(avg_progress=Avg("progress"), readers=Count("user", distinct=True))
            .order_by("chapter__order")[:20]
        )

        audio_listen_through = AudioReadingProgress.objects.filter(updated_at__gte=cutoff).aggregate(
            avg_progress=Avg("progress"), listeners=Count("user", distinct=True)
        )

        favorites_over_time = (
            Favorite.objects.filter(created_at__gte=cutoff)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )

        rating_distribution = (
            Review.objects.filter(created_at__gte=cutoff).values("rating").annotate(count=Count("id")).order_by("rating")
        )

        rating_trend = (
            Review.objects.filter(created_at__gte=cutoff)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(avg_rating=Avg("rating"), count=Count("id"))
            .order_by("day")
        )

        views_count = StoryView.objects.filter(created_at__gte=cutoff).count()
        readers_count = (
            ReadingProgress.objects.filter(updated_at__gte=cutoff).values("user_id", "story_id").distinct().count()
        )

        return Response(
            {
                "range_days": days,
                "reading_progress_buckets": reading_progress_buckets,
                "chapter_dropoff": [
                    {
                        "chapter_order": row["chapter__order"],
                        "avg_progress": round(row["avg_progress"] or 0, 3),
                        "readers": row["readers"],
                    }
                    for row in chapter_dropoff
                ],
                "audio_listen_through": {
                    "avg_progress": round(audio_listen_through["avg_progress"] or 0, 3),
                    "listeners": audio_listen_through["listeners"] or 0,
                },
                "favorites_over_time": [{"day": row["day"], "count": row["count"]} for row in favorites_over_time],
                "rating_distribution": list(rating_distribution),
                "rating_trend": [
                    {"day": row["day"], "avg_rating": round(row["avg_rating"] or 0, 2), "count": row["count"]}
                    for row in rating_trend
                ],
                "view_to_read_conversion": {
                    "views": views_count,
                    "readers": readers_count,
                    "conversion_rate": round(readers_count / views_count, 3) if views_count else 0,
                },
            }
        )


class AdminAnalyticsUsersAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        cutoff = get_cutoff(days)

        signups_over_time = (
            User.objects.filter(date_joined__gte=cutoff)
            .annotate(day=TruncDate("date_joined"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )

        active_users = User.objects.filter(last_login__gte=cutoff).count()

        login_bucket_defs = [("0", 0, 1), ("1-2", 1, 3), ("3-5", 3, 6), ("6-10", 6, 11), ("11+", 11, None)]
        login_frequency_buckets = []
        for label, lo, hi in login_bucket_defs:
            bucket_qs = User.objects.filter(login_count__gte=lo)
            if hi is not None:
                bucket_qs = bucket_qs.filter(login_count__lt=hi)
            login_frequency_buckets.append({"bucket": label, "count": bucket_qs.count()})

        joined_in_range = User.objects.filter(date_joined__gte=cutoff)
        joined_count = joined_in_range.count()
        verified_count = joined_in_range.filter(otp_verified=True).count()

        return Response(
            {
                "range_days": days,
                "signups_over_time": [{"day": row["day"], "count": row["count"]} for row in signups_over_time],
                "total_users": User.objects.count(),
                "active_users": active_users,
                "login_frequency_buckets": login_frequency_buckets,
                "otp_conversion": {
                    "joined": joined_count,
                    "verified": verified_count,
                    "rate": round(verified_count / joined_count, 3) if joined_count else 0,
                },
            }
        )


class AdminAnalyticsSubmissionsAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        cutoff = get_cutoff(days)

        submissions_qs = Submission.objects.filter(created_at__gte=cutoff)

        submissions_over_time = (
            submissions_qs.annotate(day=TruncDate("created_at"))
            .values("day", "status")
            .annotate(count=Count("id"))
            .order_by("day")
        )

        funnel_rows = list(submissions_qs.values("status").annotate(count=Count("id")))
        total = sum(row["count"] for row in funnel_rows) or 0
        funnel = [
            {
                "status": row["status"],
                "count": row["count"],
                "percent": round(row["count"] / total * 100, 1) if total else 0,
            }
            for row in funnel_rows
        ]

        reviewed_qs = submissions_qs.filter(reviewed_at__isnull=False).only("created_at", "reviewed_at")
        review_hours = [(s.reviewed_at - s.created_at).total_seconds() / 3600 for s in reviewed_qs]
        avg_time_to_review_hours = round(sum(review_hours) / len(review_hours), 1) if review_hours else 0

        by_story_type = list(submissions_qs.values("story_type").annotate(count=Count("id")).order_by("-count"))

        by_genre_qs = (
            Genre.objects.annotate(
                count=Count("submissions", filter=Q(submissions__in=submissions_qs.order_by()), distinct=True)
            )
            .filter(count__gt=0)
            .order_by("-count")
        )
        by_genre = [{"id": genre.id, "name": genre.name, "count": genre.count} for genre in by_genre_qs]

        return Response(
            {
                "range_days": days,
                "submissions_over_time": [
                    {"day": row["day"], "status": row["status"], "count": row["count"]}
                    for row in submissions_over_time
                ],
                "funnel": funnel,
                "avg_time_to_review_hours": avg_time_to_review_hours,
                "by_story_type": by_story_type,
                "by_genre": by_genre,
            }
        )


class AdminAnalyticsAudienceAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        cutoff = get_cutoff(days)
        events = AnalyticsEvent.objects.filter(created_at__gte=cutoff)

        daily_events = (
            events.annotate(day=TruncDate("created_at"))
            .values("day", "event_type")
            .annotate(count=Count("id"), duration_seconds=Sum("duration_seconds"))
            .order_by("day")
        )
        daily = {}
        for row in daily_events:
            entry = daily.setdefault(
                row["day"],
                {
                    "day": row["day"],
                    "ad_impressions": 0,
                    "downloads": 0,
                    "completions": 0,
                    "reading_minutes": 0,
                    "listening_minutes": 0,
                },
            )
            event_type = row["event_type"]
            if event_type == AnalyticsEvent.EVENT_AD_IMPRESSION:
                entry["ad_impressions"] = row["count"]
            elif event_type == AnalyticsEvent.EVENT_DOWNLOAD:
                entry["downloads"] = row["count"]
            elif event_type == AnalyticsEvent.EVENT_COMPLETION:
                entry["completions"] = row["count"]
            elif event_type == AnalyticsEvent.EVENT_READING_SESSION:
                entry["reading_minutes"] = round((row["duration_seconds"] or 0) / 60, 1)
            elif event_type == AnalyticsEvent.EVENT_LISTENING_SESSION:
                entry["listening_minutes"] = round((row["duration_seconds"] or 0) / 60, 1)

        visit_events = AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_VISIT)
        authenticated_first = {
            f"u:{row['user_id']}": row["first_seen"]
            for row in visit_events.filter(user_id__isnull=False)
            .values("user_id")
            .annotate(first_seen=Min("created_at"))
        }
        anonymous_first = {
            f"v:{row['visitor_id']}": row["first_seen"]
            for row in visit_events.filter(user_id__isnull=True)
            .values("visitor_id")
            .annotate(first_seen=Min("created_at"))
        }
        first_seen = {**anonymous_first, **authenticated_first}
        visitor_days = {}
        for user_id, visitor_id, created_at in events.filter(
            event_type=AnalyticsEvent.EVENT_VISIT
        ).values_list("user_id", "visitor_id", "created_at"):
            identity = f"u:{user_id}" if user_id else f"v:{visitor_id}"
            day = timezone.localtime(created_at).date()
            visitor_days.setdefault(day, set()).add(identity)

        new_returning = []
        all_visitors = set()
        returning_visitors = set()
        for day in sorted(visitor_days):
            new_ids = set()
            returning_ids = set()
            for identity in visitor_days[day]:
                all_visitors.add(identity)
                first = first_seen.get(identity)
                if first and timezone.localtime(first).date() < day:
                    returning_ids.add(identity)
                    returning_visitors.add(identity)
                else:
                    new_ids.add(identity)
            new_returning.append(
                {"day": day, "new_visitors": len(new_ids), "returning_visitors": len(returning_ids)}
            )

        sessions = events.filter(
            event_type__in=[
                AnalyticsEvent.EVENT_READING_SESSION,
                AnalyticsEvent.EVENT_LISTENING_SESSION,
            ]
        )
        reading_seconds = sessions.filter(
            event_type=AnalyticsEvent.EVENT_READING_SESSION
        ).aggregate(total=Sum("duration_seconds"))["total"] or 0
        listening_seconds = sessions.filter(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION
        ).aggregate(total=Sum("duration_seconds"))["total"] or 0
        # Additive breakdowns, not carve-outs — reading_seconds above already
        # sums every reading_session regardless of content (blog and quick
        # read reads were never excluded from it), same as it already did for
        # quick read (a Story) before blog tracking existed.
        blog_reading_seconds = sessions.filter(
            event_type=AnalyticsEvent.EVENT_READING_SESSION, blog_id__isnull=False
        ).aggregate(total=Sum("duration_seconds"))["total"] or 0
        quick_read_reading_seconds = sessions.filter(
            event_type=AnalyticsEvent.EVENT_READING_SESSION, metadata__format="quick_read"
        ).aggregate(total=Sum("duration_seconds"))["total"] or 0
        total_session_count = sessions.count()

        reader_days = {}
        engaged_titles = set()
        for user_id, visitor_id, story_id, created_at in sessions.values_list(
            "user_id", "visitor_id", "story_id", "created_at"
        ):
            identity = f"u:{user_id}" if user_id else f"v:{visitor_id}"
            reader_days.setdefault(identity, set()).add(timezone.localtime(created_at).date())
            if story_id:
                engaged_titles.add((identity, story_id))
        returning_readers = sum(1 for active_days in reader_days.values() if len(active_days) > 1)

        completed_titles = {
            (f"u:{user_id}" if user_id else f"v:{visitor_id}", story_id)
            for user_id, visitor_id, story_id in events.filter(
                event_type=AnalyticsEvent.EVENT_COMPLETION,
                story_id__isnull=False,
            ).values_list("user_id", "visitor_id", "story_id")
        }
        completed_engagements = len(completed_titles & engaged_titles)

        ad_placements = list(
            events.filter(event_type=AnalyticsEvent.EVENT_AD_IMPRESSION)
            .values("metadata__path", "metadata__size")
            .annotate(count=Count("id"))
            .order_by("-count")[:12]
        )
        download_types = list(
            events.filter(event_type=AnalyticsEvent.EVENT_DOWNLOAD)
            .values("metadata__content_type")
            .annotate(count=Count("id"), bytes=Sum("value"))
            .order_by("-count")
        )
        completion_types = list(
            events.filter(event_type=AnalyticsEvent.EVENT_COMPLETION)
            .values("metadata__content_type")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        # Only starts getting populated once AdSpace.tsx sends content_type
        # in metadata — existing/historical rows show up with a null key,
        # same "unattributed" handling as download_types/completion_types.
        ad_impressions_by_content_type = list(
            events.filter(event_type=AnalyticsEvent.EVENT_AD_IMPRESSION)
            .values("metadata__content_type")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        top_downloads = list(
            events.filter(event_type=AnalyticsEvent.EVENT_DOWNLOAD, story_id__isnull=False)
            .values("story_id", "story__title", "story__slug")
            .annotate(count=Count("id"), bytes=Sum("value"))
            .order_by("-count", "story__title")[:10]
        )
        top_listened = list(
            events.filter(event_type=AnalyticsEvent.EVENT_LISTENING_SESSION, story_id__isnull=False)
            .values("story_id", "story__title", "story__slug")
            .annotate(duration_seconds=Sum("duration_seconds"), sessions=Count("id"))
            .order_by("-duration_seconds")[:10]
        )
        top_blogs_read = list(
            events.filter(event_type=AnalyticsEvent.EVENT_READING_SESSION, blog_id__isnull=False)
            .values("blog_id", "blog__title", "blog__slug")
            .annotate(duration_seconds=Sum("duration_seconds"), sessions=Count("id"))
            .order_by("-duration_seconds")[:10]
        )

        impressions = events.filter(event_type=AnalyticsEvent.EVENT_AD_IMPRESSION).count()
        downloads = events.filter(event_type=AnalyticsEvent.EVENT_DOWNLOAD)
        completions = events.filter(event_type=AnalyticsEvent.EVENT_COMPLETION)
        unique_downloaders = {
            f"u:{user_id}" if user_id else f"v:{visitor_id}"
            for user_id, visitor_id in downloads.values_list("user_id", "visitor_id")
        }

        return Response(
            {
                "range_days": days,
                "summary": {
                    "visitors": len(all_visitors),
                    "returning_visitors": len(returning_visitors),
                    "returning_rate": round(len(returning_visitors) / len(all_visitors), 3)
                    if all_visitors
                    else 0,
                    "readers": len(reader_days),
                    "returning_readers": returning_readers,
                    "reader_retention_rate": round(returning_readers / len(reader_days), 3)
                    if reader_days
                    else 0,
                    "ad_impressions": impressions,
                    "downloads": downloads.count(),
                    "unique_downloaders": len(unique_downloaders),
                    "completions": completions.count(),
                    "completion_rate": round(completed_engagements / len(engaged_titles), 3)
                    if engaged_titles
                    else 0,
                    "reading_minutes": round(reading_seconds / 60, 1),
                    "listening_minutes": round(listening_seconds / 60, 1),
                    "blog_reading_minutes": round(blog_reading_seconds / 60, 1),
                    "quick_read_reading_minutes": round(quick_read_reading_seconds / 60, 1),
                    "avg_session_minutes": round(
                        (reading_seconds + listening_seconds) / total_session_count / 60, 1
                    )
                    if total_session_count
                    else 0,
                },
                "daily_activity": list(daily.values()),
                "visitor_retention": new_returning,
                "ad_placements": [
                    {
                        "path": row["metadata__path"] or "unknown",
                        "size": row["metadata__size"] or "unknown",
                        "count": row["count"],
                    }
                    for row in ad_placements
                ],
                "download_types": [
                    {
                        "content_type": row["metadata__content_type"] or "unknown",
                        "count": row["count"],
                        "bytes": round(row["bytes"] or 0),
                    }
                    for row in download_types
                ],
                "completion_types": [
                    {
                        "content_type": row["metadata__content_type"] or "unknown",
                        "count": row["count"],
                    }
                    for row in completion_types
                ],
                "ad_impressions_by_content_type": [
                    {
                        "content_type": row["metadata__content_type"] or "unknown",
                        "count": row["count"],
                    }
                    for row in ad_impressions_by_content_type
                ],
                "top_downloads": [
                    {
                        "story_id": row["story_id"],
                        "title": row["story__title"],
                        "slug": row["story__slug"],
                        "count": row["count"],
                        "bytes": round(row["bytes"] or 0),
                    }
                    for row in top_downloads
                ],
                "top_listened": [
                    {
                        "story_id": row["story_id"],
                        "title": row["story__title"],
                        "slug": row["story__slug"],
                        "sessions": row["sessions"],
                        "minutes": round((row["duration_seconds"] or 0) / 60, 1),
                    }
                    for row in top_listened
                ],
                "top_blogs_read": [
                    {
                        "blog_id": row["blog_id"],
                        "title": row["blog__title"],
                        "slug": row["blog__slug"],
                        "sessions": row["sessions"],
                        "minutes": round((row["duration_seconds"] or 0) / 60, 1),
                    }
                    for row in top_blogs_read
                ],
            }
        )

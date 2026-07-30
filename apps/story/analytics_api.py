from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.stats.models import AudioReadingProgress, ChapterReadingProgress, ReadingProgress
from apps.users.models import User

from .api import IsSuperUser
from .models import Favorite, Genre, Review, Story, StoryView, Submission

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
                stories_count=Count("stories", filter=Q(stories__is_published=True), distinct=True),
                avg_rating=Avg("stories__rating", filter=Q(stories__is_published=True)),
                total_views=Sum("stories__views", filter=Q(stories__is_published=True)),
                total_favorites=Count("stories__favorites", filter=Q(stories__is_published=True), distinct=True),
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
            Story.objects.filter(is_published=True)
            .values("story_type")
            .annotate(count=Count("id"), avg_rating=Avg("rating"), avg_views=Avg("views"))
            .order_by("-count")
        )

        completion_split = (
            Story.objects.filter(is_published=True)
            .values("is_completed")
            .annotate(count=Count("id"), avg_rating=Avg("rating"), avg_views=Avg("views"))
        )

        publishing_over_time = (
            Story.objects.filter(is_published=True, site_published_date__gte=cutoff.date())
            .values("site_published_date")
            .annotate(count=Count("id"))
            .order_by("site_published_date")
        )

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

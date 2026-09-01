import csv
import io
import json
import statistics
import zipfile
from datetime import datetime, timedelta

from django.db.models import Avg, Count, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from openpyxl import Workbook
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.stats.models import (
    AnalyticsEvent,
    AudioReadingProgress,
    VideoWatchProgress,
    BlogReadingProgress,
    ChapterReadingProgress,
    ReadingProgress,
)
from apps.users.models import User, UserLoginLocation

from .api import IsSuperUser
from .models import Blog, Favorite, Genre, Review, Story, StoryView, Submission, published_blog_q, published_story_q

ALLOWED_RANGE_DAYS = (1, 7, 30, 90, 365)
DEFAULT_RANGE_DAYS = 30
CACHE_SECONDS = 60 * 5
CONTENT_RANKING_PAGE_SIZE = 25


def get_range_days(request):
    try:
        days = int(request.query_params.get("days", DEFAULT_RANGE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RANGE_DAYS
    return days if days in ALLOWED_RANGE_DAYS else DEFAULT_RANGE_DAYS


def get_cutoff(days):
    return timezone.now() - timedelta(days=days)


def _content_identity(user_id, visitor_id):
    return f"u:{user_id}" if user_id else f"v:{visitor_id}"


def build_content_rankings(days, kind):
    """Return comparable, interval-scoped metrics for every published title.

    The score intentionally uses understandable raw weights rather than a
    hidden statistical model: a view is 1 point, a read is 2, an interaction
    is 3, and each engaged minute is 1. Raw values are returned alongside it
    so admins can judge performance without relying on the score alone.
    """
    cutoff = get_cutoff(days)
    is_story = kind in {"story", "audiobook"}
    title_queryset = Story.objects.published() if is_story else Blog.objects.published()
    if kind == "audiobook":
        title_queryset = title_queryset.filter(audios__isnull=False).distinct()
    titles = list(title_queryset.values("id", "title", "slug").order_by("title"))
    title_ids = [row["id"] for row in titles]
    metrics = {
        title_id: {
            "views": 0,
            "reads": 0,
            "reader_ids": set(),
            "listens": 0,
            "listener_ids": set(),
            "reading_seconds": 0,
            "listening_seconds": 0,
            "watching_seconds": 0,
            "completions": 0,
            "downloads": 0,
            "event_interactions": 0,
        }
        for title_id in title_ids
    }

    if is_story:
        for row in (
            StoryView.objects.filter(story_id__in=title_ids, created_at__gte=cutoff)
            .values("story_id")
            .annotate(count=Count("id"))
        ):
            metrics[row["story_id"]]["views"] = row["count"]

    events = AnalyticsEvent.objects.filter(created_at__gte=cutoff)
    events = events.filter(story_id__in=title_ids) if is_story else events.filter(blog_id__in=title_ids)
    id_field = "story_id" if is_story else "blog_id"
    event_interaction_types = {
        AnalyticsEvent.EVENT_COMPLETION,
        AnalyticsEvent.EVENT_DOWNLOAD,
        AnalyticsEvent.EVENT_READ_ALONG_CUE_SEEK,
        AnalyticsEvent.EVENT_READ_ALONG_FOLLOW_TOGGLE,
    }
    for title_id, event_type, user_id, visitor_id, duration in events.values_list(
        id_field, "event_type", "user_id", "visitor_id", "duration_seconds"
    ):
        row = metrics[title_id]
        duration = duration or 0
        if not is_story and event_type == AnalyticsEvent.EVENT_VISIT:
            row["views"] += 1
        elif event_type == AnalyticsEvent.EVENT_READING_SESSION:
            row["reads"] += 1
            row["reader_ids"].add(_content_identity(user_id, visitor_id))
            row["reading_seconds"] += duration
        elif event_type == AnalyticsEvent.EVENT_LISTENING_SESSION:
            row["listens"] += 1
            row["listener_ids"].add(_content_identity(user_id, visitor_id))
            row["listening_seconds"] += duration
        elif event_type == AnalyticsEvent.EVENT_WATCHING_SESSION:
            row["watching_seconds"] += duration
        if event_type == AnalyticsEvent.EVENT_COMPLETION:
            row["completions"] += 1
        if event_type == AnalyticsEvent.EVENT_DOWNLOAD:
            row["downloads"] += 1
        if event_type in event_interaction_types:
            row["event_interactions"] += 1

    favorites = {}
    reviews = {}
    if is_story:
        favorites = dict(
            Favorite.objects.filter(story_id__in=title_ids, created_at__gte=cutoff)
            .values_list("story_id")
            .annotate(count=Count("id"))
        )
        reviews = dict(
            Review.objects.filter(story_id__in=title_ids, created_at__gte=cutoff)
            .values_list("story_id")
            .annotate(count=Count("id"))
        )

    result = []
    for title in titles:
        row = metrics[title["id"]]
        favorite_count = favorites.get(title["id"], 0)
        review_count = reviews.get(title["id"], 0)
        interactions = row["event_interactions"] + favorite_count + review_count
        engagement_seconds = (
            row["reading_seconds"] + row["listening_seconds"] + row["watching_seconds"]
        )
        engagement_minutes = round(engagement_seconds / 60, 1)
        listening_minutes = round(row["listening_seconds"] / 60, 1)
        performance_score = (
            row["views"] + row["listens"] * 2 + interactions * 3 + listening_minutes
            if kind == "audiobook"
            else row["views"] + row["reads"] * 2 + interactions * 3 + engagement_minutes
        )
        result.append(
            {
                **title,
                "content_type": kind,
                "views": row["views"],
                "reads": row["reads"],
                "unique_readers": len(row["reader_ids"]),
                "listens": row["listens"],
                "unique_listeners": len(row["listener_ids"]),
                "reading_minutes": round(row["reading_seconds"] / 60, 1),
                "listening_minutes": listening_minutes,
                "watching_minutes": round(row["watching_seconds"] / 60, 1),
                "engagement_minutes": engagement_minutes,
                "interactions": interactions,
                "completions": row["completions"],
                "downloads": row["downloads"],
                "favorites": favorite_count,
                "reviews": review_count,
                "performance_score": round(performance_score, 1),
            }
        )
    return result


CONTENT_RANKING_SORTS = {
    "performance_score",
    "views",
    "reads",
    "unique_readers",
    "listens",
    "unique_listeners",
    "reading_minutes",
    "listening_minutes",
    "engagement_minutes",
    "interactions",
    "completions",
}


def sort_content_rankings(rows, sort):
    sort = sort if sort in CONTENT_RANKING_SORTS else "performance_score"
    return sorted(rows, key=lambda row: (-row[sort], row["title"].lower()))


def build_detail_time_series(days, *, story=None, blog=None):
    """Build exact rolling hourly/daily buckets for one title."""
    now = timezone.now()
    cutoff = get_cutoff(days)
    interval = "hour" if days == 1 else "day"
    step = timedelta(hours=1) if interval == "hour" else timedelta(days=1)
    bucket_count = 24 if interval == "hour" else days
    points = [
        {
            "period": (cutoff + step * index).isoformat(),
            "views": 0,
            "reads": 0,
            "reading_minutes": 0.0,
            "listens": 0,
            "listening_minutes": 0.0,
            "read_along_listens": 0,
            "read_along_minutes": 0.0,
            "interactions": 0,
        }
        for index in range(bucket_count)
    ]

    def bucket_for(created_at):
        index = int((created_at - cutoff).total_seconds() // step.total_seconds())
        return points[index] if 0 <= index < bucket_count else None

    if story is not None:
        for created_at in story.view_events.filter(
            created_at__gte=cutoff, created_at__lte=now
        ).values_list("created_at", flat=True):
            bucket = bucket_for(created_at)
            if bucket:
                bucket["views"] += 1

    events = AnalyticsEvent.objects.filter(created_at__gte=cutoff, created_at__lte=now)
    events = events.filter(story=story) if story is not None else events.filter(blog=blog)
    interaction_types = {
        AnalyticsEvent.EVENT_COMPLETION,
        AnalyticsEvent.EVENT_DOWNLOAD,
        AnalyticsEvent.EVENT_READ_ALONG_CUE_SEEK,
        AnalyticsEvent.EVENT_READ_ALONG_FOLLOW_TOGGLE,
    }
    for event_type, duration, metadata, created_at in events.values_list(
        "event_type", "duration_seconds", "metadata", "created_at"
    ):
        bucket = bucket_for(created_at)
        if not bucket:
            continue
        if blog is not None and event_type == AnalyticsEvent.EVENT_VISIT:
            bucket["views"] += 1
        if event_type == AnalyticsEvent.EVENT_READING_SESSION:
            bucket["reads"] += 1
            bucket["reading_minutes"] += (duration or 0) / 60
        if event_type == AnalyticsEvent.EVENT_LISTENING_SESSION:
            bucket["listens"] += 1
            bucket["listening_minutes"] += (duration or 0) / 60
            if (metadata or {}).get("format") == "read_along":
                bucket["read_along_listens"] += 1
                bucket["read_along_minutes"] += (duration or 0) / 60
        if event_type in interaction_types:
            bucket["interactions"] += 1

    if story is not None:
        for queryset in (
            story.favorites.filter(created_at__gte=cutoff, created_at__lte=now),
            story.reviews.filter(created_at__gte=cutoff, created_at__lte=now),
        ):
            for created_at in queryset.values_list("created_at", flat=True):
                bucket = bucket_for(created_at)
                if bucket:
                    bucket["interactions"] += 1

    for point in points:
        point["reading_minutes"] = round(point["reading_minutes"], 1)
        point["listening_minutes"] = round(point["listening_minutes"], 1)
        point["read_along_minutes"] = round(point["read_along_minutes"], 1)
    return {"interval": interval, "points": points}


# Each of these build_*_data(days) functions holds the actual query logic
# for one analytics section, returning a plain dict — the section's own
# APIView.get() just wraps it in a Response, and AdminAnalyticsExportAPIView
# calls the same functions directly. Keeping the queries in one place means
# the CSV/Excel export can never drift out of sync with what the dashboard
# itself shows.


def build_content_data(days):
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
        .values("story_type__name")
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
    stories_count = Story.objects.published().count()
    # Same "has_audio"/"has_summary" semantics as StoryFilter (filters.py)
    # — a story counts as an audiobook/Quick Read if it has narration
    # audio / a summary, matching how those are surfaced to readers.
    audiobooks_count = Story.objects.published().filter(audios__isnull=False).distinct().count()
    watchable_count = Story.objects.published().filter(videos__isnull=False).distinct().count()
    quick_read_count = (
        Story.objects.published().exclude(Q(summary__isnull=True) | Q(summary__exact="")).count()
    )
    top_stories = sort_content_rankings(build_content_rankings(days, "story"), "performance_score")[:5]
    top_audiobooks = sort_content_rankings(
        build_content_rankings(days, "audiobook"), "performance_score"
    )[:5]
    top_blogs = sort_content_rankings(build_content_rankings(days, "blog"), "performance_score")[:5]

    return {
        "range_days": days,
        "views_over_time": [{"day": row["day"], "count": row["count"]} for row in views_over_time],
        "genre_performance": genre_performance,
        "story_type_breakdown": [
            {
                "story_type": row["story_type__name"],
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
        "stories_count": stories_count,
        "audiobooks_count": audiobooks_count,
        "watchable_count": watchable_count,
        "quick_read_count": quick_read_count,
        "top_stories": top_stories,
        "top_audiobooks": top_audiobooks,
        "top_blogs": top_blogs,
    }


def build_engagement_data(days):
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

    video_watch_through = VideoWatchProgress.objects.filter(updated_at__gte=cutoff).aggregate(
        avg_progress=Avg("progress"), watchers=Count("user", distinct=True)
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

    return {
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
        "video_watch_through": {
            "avg_progress": round(video_watch_through["avg_progress"] or 0, 3),
            "watchers": video_watch_through["watchers"] or 0,
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


def build_users_data(days):
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

    return {
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


def build_geography_data(days):
    cutoff = get_cutoff(days)

    logins = UserLoginLocation.objects.filter(created_at__gte=cutoff)
    resolved = logins.exclude(country="")

    by_country = (
        resolved.values("country", "country_code")
        .annotate(logins=Count("id"), users=Count("user", distinct=True))
        .order_by("-users", "-logins")
    )

    by_city = (
        resolved.exclude(city="")
        .values("city", "country")
        .annotate(logins=Count("id"), users=Count("user", distinct=True))
        .order_by("-users", "-logins")[:20]
    )

    logins_over_time = (
        logins.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"), users=Count("user", distinct=True))
        .order_by("day")
    )

    total_logins = logins.count()

    return {
        "range_days": days,
        "total_logins": total_logins,
        # Logins ip-api.com couldn't resolve (private/local IP,
        # provider timeout/outage) — surfaced so a suspiciously high
        # share is visible rather than silently missing from the map.
        "unresolved_logins": total_logins - resolved.count(),
        "countries_reached": by_country.count(),
        "by_country": [
            {
                "country": row["country"],
                "country_code": row["country_code"],
                "logins": row["logins"],
                "users": row["users"],
            }
            for row in by_country
        ],
        "by_city": [
            {
                "city": row["city"],
                "country": row["country"],
                "logins": row["logins"],
                "users": row["users"],
            }
            for row in by_city
        ],
        "logins_over_time": [
            {"day": row["day"], "count": row["count"], "users": row["users"]} for row in logins_over_time
        ],
    }


def build_submissions_data(days):
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

    by_story_type = [
        {"story_type": row["story_type__name"], "count": row["count"]}
        for row in submissions_qs.values("story_type__name").annotate(count=Count("id")).order_by("-count")
    ]

    by_genre_qs = (
        Genre.objects.annotate(
            count=Count("submissions", filter=Q(submissions__in=submissions_qs.order_by()), distinct=True)
        )
        .filter(count__gt=0)
        .order_by("-count")
    )
    by_genre = [{"id": genre.id, "name": genre.name, "count": genre.count} for genre in by_genre_qs]

    return {
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


def build_audience_data(days):
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
                "watching_minutes": 0,
                "read_along_minutes": 0,
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
        elif event_type == AnalyticsEvent.EVENT_WATCHING_SESSION:
            entry["watching_minutes"] = round((row["duration_seconds"] or 0) / 60, 1)

    # `daily_events` groups by event_type only, so the Read Along slice of
    # listening_session needs its own pass. Each such day already has an entry
    # (the same rows fed listening_minutes above); setdefault is just defensive.
    for row in (
        events.filter(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION,
            metadata__format="read_along",
        )
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(duration_seconds=Sum("duration_seconds"))
    ):
        entry = daily.setdefault(
            row["day"],
            {
                "day": row["day"],
                "ad_impressions": 0,
                "downloads": 0,
                "completions": 0,
                "reading_minutes": 0,
                "listening_minutes": 0,
                "watching_minutes": 0,
                "read_along_minutes": 0,
            },
        )
        entry["read_along_minutes"] = round((row["duration_seconds"] or 0) / 60, 1)

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
    page_view_counts = {}
    page_unique_visitors = {}
    # Every visit timestamp per browser tab session — summing the gaps
    # between consecutive page loads (capped, so a tab left open for
    # hours doesn't register as one enormous "active" gap) approximates
    # how long that visit actually browsed the site, distinct from
    # reading_minutes/listening_minutes above, which only count time
    # spent inside a reader/player specifically.
    session_timestamps = {}
    for user_id, visitor_id, session_id, created_at, metadata in events.filter(
        event_type=AnalyticsEvent.EVENT_VISIT
    ).values_list("user_id", "visitor_id", "session_id", "created_at", "metadata"):
        identity = f"u:{user_id}" if user_id else f"v:{visitor_id}"
        day = timezone.localtime(created_at).date()
        visitor_days.setdefault(day, set()).add(identity)

        path = (metadata or {}).get("path") or "unknown"
        page_view_counts[path] = page_view_counts.get(path, 0) + 1
        page_unique_visitors.setdefault(path, set()).add(identity)

        if session_id:
            session_timestamps.setdefault(session_id, []).append(created_at)

    top_pages = sorted(
        (
            {"path": path, "views": count, "unique_visitors": len(page_unique_visitors[path])}
            for path, count in page_view_counts.items()
        ),
        key=lambda row: row["views"],
        reverse=True,
    )[:20]
    total_page_views = sum(page_view_counts.values())

    # A gap over 30 minutes between page loads reads as the tab having
    # been left open/idle rather than active browsing, so it's excluded
    # from the sum rather than inflating the session's duration.
    IDLE_GAP_SECONDS = 30 * 60
    browsing_session_seconds = []
    for timestamps in session_timestamps.values():
        timestamps.sort()
        active_seconds = sum(
            min((b - a).total_seconds(), IDLE_GAP_SECONDS) for a, b in zip(timestamps, timestamps[1:])
        )
        if active_seconds > 0:
            browsing_session_seconds.append(active_seconds)
    # Median rather than mean — a handful of very long, mostly-idle
    # sessions would otherwise dominate the average even after the
    # per-gap cap above, since a session can still have many small
    # active gaps that add up over a long visit.
    median_browsing_session_minutes = (
        round(statistics.median(browsing_session_seconds) / 60, 1) if browsing_session_seconds else 0
    )

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
            AnalyticsEvent.EVENT_WATCHING_SESSION,
        ]
    )
    reading_seconds = sessions.filter(
        event_type=AnalyticsEvent.EVENT_READING_SESSION
    ).aggregate(total=Sum("duration_seconds"))["total"] or 0
    listening_seconds = sessions.filter(
        event_type=AnalyticsEvent.EVENT_LISTENING_SESSION
    ).aggregate(total=Sum("duration_seconds"))["total"] or 0
    watching_seconds = sessions.filter(
        event_type=AnalyticsEvent.EVENT_WATCHING_SESSION
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
    # Read Along is a listening surface — its time is already inside
    # listening_seconds; surfaced separately so it isn't merged into audiobook
    # listening (or into text reading).
    read_along_sessions_qs = sessions.filter(
        event_type=AnalyticsEvent.EVENT_LISTENING_SESSION, metadata__format="read_along"
    )
    read_along_listening_seconds = read_along_sessions_qs.aggregate(
        total=Sum("duration_seconds")
    )["total"] or 0
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
    referral_sources = list(
        events.filter(event_type=AnalyticsEvent.EVENT_VISIT)
        .values("metadata__referral_source")
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
    top_read_along = list(
        events.filter(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION,
            metadata__format="read_along",
            story_id__isnull=False,
        )
        .values("story_id", "story__title", "story__slug")
        .annotate(duration_seconds=Sum("duration_seconds"), sessions=Count("id"))
        .order_by("-duration_seconds")[:10]
    )
    top_watched = list(
        events.filter(event_type=AnalyticsEvent.EVENT_WATCHING_SESSION, story_id__isnull=False)
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

    return {
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
            "watching_minutes": round(watching_seconds / 60, 1),
            "blog_reading_minutes": round(blog_reading_seconds / 60, 1),
            "quick_read_reading_minutes": round(quick_read_reading_seconds / 60, 1),
            "read_along_listening_minutes": round(read_along_listening_seconds / 60, 1),
            "read_along_sessions": read_along_sessions_qs.count(),
            "avg_session_minutes": round(
                (reading_seconds + listening_seconds + watching_seconds) / total_session_count / 60, 1
            )
            if total_session_count
            else 0,
            "total_page_views": total_page_views,
            "median_browsing_session_minutes": median_browsing_session_minutes,
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
        "referral_sources": [
            {
                "referral_source": row["metadata__referral_source"] or "direct",
                "count": row["count"],
            }
            for row in referral_sources
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
        "top_read_along": [
            {
                "story_id": row["story_id"],
                "title": row["story__title"],
                "slug": row["story__slug"],
                "sessions": row["sessions"],
                "minutes": round((row["duration_seconds"] or 0) / 60, 1),
            }
            for row in top_read_along
        ],
        "top_watched": [
            {
                "story_id": row["story_id"],
                "title": row["story__title"],
                "slug": row["story__slug"],
                "sessions": row["sessions"],
                "minutes": round((row["duration_seconds"] or 0) / 60, 1),
            }
            for row in top_watched
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
        "top_pages": top_pages,
    }


class AdminAnalyticsContentAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_content_data(get_range_days(request)))


class AdminAnalyticsContentRankingsAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        kind = request.query_params.get("kind", "story")
        if kind not in {"story", "audiobook", "blog"}:
            kind = "story"
        sort = request.query_params.get("sort", "performance_score")
        if sort not in CONTENT_RANKING_SORTS:
            sort = "performance_score"
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        rows = sort_content_rankings(build_content_rankings(days, kind), sort)
        count = len(rows)
        start = (page - 1) * CONTENT_RANKING_PAGE_SIZE
        end = start + CONTENT_RANKING_PAGE_SIZE
        return Response(
            {
                "range_days": days,
                "content_type": kind,
                "sort": sort,
                "count": count,
                "page": page,
                "page_size": CONTENT_RANKING_PAGE_SIZE,
                "results": rows[start:end],
            }
        )


class AdminAnalyticsEngagementAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_engagement_data(get_range_days(request)))


class AdminAnalyticsUsersAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_users_data(get_range_days(request)))


class AdminAnalyticsGeographyAPIView(APIView):
    """Where users are signing in from — powers the admin panel's country
    heatmap and city breakdown. Backed by UserLoginLocation, one row per
    login, populated by apps.users.geo.record_login on every real login."""

    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_geography_data(get_range_days(request)))


class AdminAnalyticsSubmissionsAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_submissions_data(get_range_days(request)))


class AdminAnalyticsAudienceAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_audience_data(get_range_days(request)))


EXPORT_SECTION_BUILDERS = {
    "content": build_content_data,
    "engagement": build_engagement_data,
    "audience": build_audience_data,
    "users": build_users_data,
    "geography": build_geography_data,
    "submissions": build_submissions_data,
}


def _json_safe(value):
    # Both csv.DictWriter and openpyxl's cell writer need a string, number,
    # bool, date/datetime, or None — the only field shapes that don't
    # already satisfy that are the rare list/dict value (e.g. a metadata
    # blob that slipped through), which get serialized rather than
    # crashing the export.
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


# Which top-level keys in each section's response dict are "table" fields
# (always exported as their own sheet/CSV, even with zero rows) rather than
# scalar/nested-dict fields that fold into a one-row "summary" table.
# Declared explicitly instead of inferred from the data itself — an empty
# list and "not a table field" look identical at runtime (both are just
# `[]`), so inferring from content silently dropped a table whenever it
# happened to have no rows for the selected date range (e.g. no reviews in
# the last 7 days made "rating_trend" vanish from the export instead of
# showing up as an empty sheet).
SECTION_TABLE_KEYS = {
    "content": {
        "views_over_time",
        "genre_performance",
        "story_type_breakdown",
        "completion_split",
        "publishing_over_time",
        "blog_publishing_over_time",
    },
    "engagement": {
        "reading_progress_buckets",
        "chapter_dropoff",
        "favorites_over_time",
        "rating_distribution",
        "rating_trend",
    },
    "users": {"signups_over_time", "login_frequency_buckets"},
    "geography": {"by_country", "by_city", "logins_over_time"},
    "submissions": {"submissions_over_time", "funnel", "by_story_type", "by_genre"},
    "audience": {
        "daily_activity",
        "visitor_retention",
        "ad_placements",
        "download_types",
        "completion_types",
        "ad_impressions_by_content_type",
        "referral_sources",
        "top_downloads",
        "top_listened",
        "top_read_along",
        "top_watched",
        "top_blogs_read",
        "top_pages",
    },
}


def _flatten_to_tables(section, data):
    """Splits one section's response dict into (table_name, rows) pairs,
    per SECTION_TABLE_KEYS above; every other field (scalar, or a
    "summary"-shaped nested dict like view_to_read_conversion) folds into a
    single one-row "summary" table."""
    table_keys = SECTION_TABLE_KEYS.get(section, set())
    tables = []
    summary_row = {}
    for key, value in data.items():
        if key in table_keys:
            tables.append((key, [{k: _json_safe(v) for k, v in row.items()} for row in value]))
        elif isinstance(value, dict):
            for subkey, subval in value.items():
                summary_row[f"{key}.{subkey}"] = _json_safe(subval)
        elif isinstance(value, list):
            summary_row[key] = ", ".join(str(v) for v in value) if value else ""
        else:
            summary_row[key] = _json_safe(value)
    if summary_row:
        tables.insert(0, ("summary", [summary_row]))
    return tables


def _rows_to_csv_bytes(rows):
    if not rows:
        return b""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 CSVs correctly


def _unique_name(base, used, max_length=None):
    name = base if max_length is None else base[:max_length]
    counter = 1
    while name in used:
        counter += 1
        suffix = f"_{counter}"
        name = (base[: (max_length or len(base)) - len(suffix)] if max_length else base) + suffix
    used.add(name)
    return name


class AdminAnalyticsExportAPIView(APIView):
    """Lets an admin pick any combination of the six analytics sections and
    download them as a single file — one sheet per table in an .xlsx
    workbook, or a .csv (a .zip of .csv files if more than one table is
    involved, since CSV has no notion of multiple tables in one file)."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        # Named "file_format", not "format" — DRF reserves the bare
        # "?format=" query parameter for its own content-negotiation
        # override (e.g. "?format=json"/"?format=api"); using it here for
        # "csv" vs "xlsx" made DRF try to find a renderer called "csv" or
        # "xlsx", fail, and raise a raw Http404 before this method body
        # ever ran — this endpoint returning a 404 for every single request
        # regardless of the actual export logic.
        export_format = (request.query_params.get("file_format") or "xlsx").lower()
        if export_format not in ("csv", "xlsx"):
            return Response({"detail": "file_format must be 'csv' or 'xlsx'."}, status=400)

        requested = [s.strip() for s in (request.query_params.get("sections") or "").split(",") if s.strip()]
        sections = [s for s in requested if s in EXPORT_SECTION_BUILDERS]
        if not sections:
            return Response(
                {"detail": f"sections must include at least one of: {', '.join(EXPORT_SECTION_BUILDERS)}."},
                status=400,
            )

        days = get_range_days(request)

        section_tables = []
        for section in sections:
            data = EXPORT_SECTION_BUILDERS[section](days)
            for table_name, rows in _flatten_to_tables(section, data):
                section_tables.append((section, table_name, rows))

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        if export_format == "xlsx":
            workbook = Workbook()
            workbook.remove(workbook.active)
            used_sheet_names = set()
            for section, table_name, rows in section_tables:
                sheet = workbook.create_sheet(title=_unique_name(f"{section}_{table_name}", used_sheet_names, 31))
                if rows:
                    headers = list(rows[0].keys())
                    sheet.append(headers)
                    for row in rows:
                        sheet.append([row.get(header) for header in headers])
            buffer = io.BytesIO()
            workbook.save(buffer)
            response = HttpResponse(
                buffer.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            response["Content-Disposition"] = f'attachment; filename="analytics-export-{timestamp}.xlsx"'
            return response

        # CSV: a single table downloads directly; multiple tables (any
        # section with more than one list field, or more than one section
        # selected) are bundled into a zip, one .csv per table.
        if len(section_tables) == 1:
            _, _, rows = section_tables[0]
            response = HttpResponse(_rows_to_csv_bytes(rows), content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="analytics-export-{timestamp}.csv"'
            return response

        zip_buffer = io.BytesIO()
        used_filenames = set()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for section, table_name, rows in section_tables:
                filename = _unique_name(f"{section}_{table_name}", used_filenames) + ".csv"
                archive.writestr(filename, _rows_to_csv_bytes(rows))
        response = HttpResponse(zip_buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="analytics-export-{timestamp}.zip"'
        return response


# ----------------------------------------------------------------------------
# Per-title analytics — how one specific story or blog post is doing, rather
# than a site-wide aggregate. Reuses the same time-range filtering as the
# aggregate endpoints above, scoped down to a single title's data.
# ----------------------------------------------------------------------------


def build_story_detail_data(story, days):
    cutoff = get_cutoff(days)

    # StoryView is throttled/deduped at write time (one row per IP per
    # dedupe window — see its own docstring in models.py), so a plain count
    # already approximates distinct viewers; deduping again here on top of
    # that by (user, ip) catches the case of the same visitor crossing that
    # window more than once in the selected range.
    page_opens = (
        story.view_events.filter(created_at__gte=cutoff)
        .values("user_id", "ip_address")
        .distinct()
        .count()
    )

    progress_qs = ReadingProgress.objects.filter(story=story, updated_at__gte=cutoff)
    started_reading = progress_qs.filter(progress__gt=0).count()
    completed_reading = progress_qs.filter(progress__gte=0.99).count()
    avg_progress = progress_qs.aggregate(avg=Avg("progress"))["avg"] or 0

    chapter_breakdown = (
        ChapterReadingProgress.objects.filter(story=story, updated_at__gte=cutoff)
        .values("chapter__order", "chapter__title", "chapter__slug")
        .annotate(
            readers=Count("user", distinct=True),
            avg_progress=Avg("progress"),
            completed=Count("user", filter=Q(progress__gte=0.99), distinct=True),
        )
        .order_by("chapter__order")
    )

    events = AnalyticsEvent.objects.filter(story=story, created_at__gte=cutoff)
    reading_seconds = events.filter(event_type=AnalyticsEvent.EVENT_READING_SESSION).aggregate(
        total=Sum("duration_seconds")
    )["total"] or 0
    completions_tracked = events.filter(event_type=AnalyticsEvent.EVENT_COMPLETION).count()

    has_audio = story.audios.exists()
    audio_data = None
    if has_audio:
        audio_progress_qs = AudioReadingProgress.objects.filter(story=story, updated_at__gte=cutoff)
        listening_events = events.filter(event_type=AnalyticsEvent.EVENT_LISTENING_SESSION)
        listening_seconds = listening_events.aggregate(total=Sum("duration_seconds"))["total"] or 0
        read_along_seconds = listening_events.filter(metadata__format="read_along").aggregate(
            total=Sum("duration_seconds")
        )["total"] or 0
        audio_data = {
            "listeners": audio_progress_qs.values("user_id").distinct().count(),
            "avg_progress": round(audio_progress_qs.aggregate(avg=Avg("progress"))["avg"] or 0, 3),
            "listening_minutes": round(listening_seconds / 60, 1),
            "read_along_listening_minutes": round(read_along_seconds / 60, 1),
        }

    has_video = story.videos.exists()
    video_data = None
    if has_video:
        video_progress_qs = VideoWatchProgress.objects.filter(story=story, updated_at__gte=cutoff)
        watching_seconds = events.filter(event_type=AnalyticsEvent.EVENT_WATCHING_SESSION).aggregate(
            total=Sum("duration_seconds")
        )["total"] or 0
        video_data = {
            "watchers": video_progress_qs.values("user_id").distinct().count(),
            "avg_progress": round(video_progress_qs.aggregate(avg=Avg("progress"))["avg"] or 0, 3),
            "watching_minutes": round(watching_seconds / 60, 1),
        }

    return {
        "range_days": days,
        "story": {"id": story.id, "title": story.title, "slug": story.slug},
        "time_series": build_detail_time_series(days, story=story),
        "page_opens": page_opens,
        "started_reading": started_reading,
        "completed_reading": completed_reading,
        "avg_progress": round(avg_progress, 3),
        "reading_minutes": round(reading_seconds / 60, 1),
        "completions_tracked": completions_tracked,
        "favorites_count": story.favorites.filter(created_at__gte=cutoff).count(),
        "reviews_count": story.reviews.filter(created_at__gte=cutoff).count(),
        "avg_rating_in_range": round(
            story.reviews.filter(created_at__gte=cutoff).aggregate(avg=Avg("rating"))["avg"] or 0, 2
        ),
        "chapter_breakdown": [
            {
                "chapter_order": row["chapter__order"],
                "chapter_title": row["chapter__title"],
                "chapter_slug": row["chapter__slug"],
                "readers": row["readers"],
                "avg_progress": round(row["avg_progress"] or 0, 3),
                "completed": row["completed"],
            }
            for row in chapter_breakdown
        ],
        "has_audio": has_audio,
        "audio": audio_data,
        "has_video": has_video,
        "video": video_data,
    }


def build_blog_detail_data(blog, days):
    cutoff = get_cutoff(days)

    visit_qs = AnalyticsEvent.objects.filter(
        event_type=AnalyticsEvent.EVENT_VISIT,
        metadata__path=f"/blog/{blog.slug}",
        created_at__gte=cutoff,
    )
    page_open_identities = {
        f"u:{user_id}" if user_id else f"v:{visitor_id}"
        for user_id, visitor_id in visit_qs.values_list("user_id", "visitor_id")
    }

    reading_sessions = AnalyticsEvent.objects.filter(
        event_type=AnalyticsEvent.EVENT_READING_SESSION, blog=blog, created_at__gte=cutoff
    )
    reader_identities = {
        f"u:{user_id}" if user_id else f"v:{visitor_id}"
        for user_id, visitor_id in reading_sessions.values_list("user_id", "visitor_id")
    }
    reading_seconds = reading_sessions.aggregate(total=Sum("duration_seconds"))["total"] or 0

    # Scroll-depth progress: authenticated readers only (BlogReadingProgress
    # has the same limitation ReadingProgress already has for stories — see
    # its docstring), so this is a subset of reader_identities above, not
    # the full picture. Surfaced separately and clearly labeled rather than
    # blended into "started reading", which stays anonymous-inclusive.
    depth_qs = BlogReadingProgress.objects.filter(blog=blog, updated_at__gte=cutoff)
    bucket_defs = [
        ("0-25%", 0.0, 0.25),
        ("25-50%", 0.25, 0.5),
        ("50-75%", 0.5, 0.75),
        ("75-100%", 0.75, 1.01),
    ]
    progress_distribution = [
        {"bucket": label, "count": depth_qs.filter(progress__gte=lo, progress__lt=hi).count()}
        for label, lo, hi in bucket_defs
    ]

    return {
        "range_days": days,
        "blog": {"id": blog.id, "title": blog.title, "slug": blog.slug},
        "time_series": build_detail_time_series(days, blog=blog),
        "page_opens": len(page_open_identities),
        "started_reading": len(reader_identities),
        "reading_minutes": round(reading_seconds / 60, 1),
        "signed_in_readers_with_depth_tracked": depth_qs.count(),
        "avg_progress_signed_in": round(depth_qs.aggregate(avg=Avg("progress"))["avg"] or 0, 3),
        "completed_signed_in": depth_qs.filter(progress__gte=0.99).count(),
        "progress_distribution_signed_in": progress_distribution,
    }


class AdminStoryDetailAnalyticsAPIView(APIView):
    """One specific story's engagement, as opposed to the site-wide
    aggregates above — page opens, who started reading, how far they got,
    and a per-chapter breakdown of exactly where readers dropped off."""

    permission_classes = [IsSuperUser]

    def get(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        return Response(build_story_detail_data(story, get_range_days(request)))


class AdminBlogDetailAnalyticsAPIView(APIView):
    """One specific blog post's engagement — see build_blog_detail_data's
    docstring-equivalent comment for why the depth/drop-off metrics are
    scoped to signed-in readers only."""

    permission_classes = [IsSuperUser]

    def get(self, request, blog_slug):
        blog = get_object_or_404(Blog.objects.published(), slug=blog_slug)
        return Response(build_blog_detail_data(blog, get_range_days(request)))

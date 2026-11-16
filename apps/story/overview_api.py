"""The admin Overview dashboard: how the site did this period vs the one
before, plus what needs an admin's attention.

Deep analysis lives in analytics_api; this module summarises it and reuses its
helpers (audience filtering, range parsing, bucket filling, content rankings,
referral sources) rather than re-deriving them. Every audience metric goes
through audience_only()/audience_events(), so operator activity and suspected
bots never reach the numbers.

Windows are explicit [start, end) pairs rather than build_with_previous_period:
that helper patches timezone.now() but the builders it wraps only have a lower
bound, so its "previous" figures would still include the current period.
"""
import statistics
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Case, CharField, Count, Max, Q, Sum, Value, When
from django.db.models.functions import Cast, Concat
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.feedback.models import Feedback
from apps.stats.models import (
    AnalyticsEvent,
    AudioReadingProgress,
    ChapterReadingProgress,
    VideoWatchProgress,
)
from apps.users.models import User

from .analytics_api import (
    ALLOWED_RANGE_DAYS,
    CACHE_SECONDS,
    audience_events,
    audience_only,
    build_content_rankings,
    build_publish_markers,
    build_referral_sources,
    build_story_lifecycle_counts,
    _rate,
    fill_time_buckets,
    filter_since,
    get_cutoff,
    get_range_days,
    get_time_interval,
    python_time_bucket,
    sort_content_rankings,
    time_trunc,
)
from .api import IsSuperUser
from .content_scoring import SessionCaps
from .models import (
    Audio,
    BookFetchJob,
    Chapter,
    EpubImportJob,
    Story,
    StoryQueue,
    Submission,
    Video,
    published_story_q,
)

OVERVIEW_DEFAULT_RANGE_DAYS = 7
OVERVIEW_CACHE_KEY = "admin-overview:v1:{days}"

MOVERS_LIMIT = 5
# A story needs this many starts before its completion rate means anything.
HIDDEN_GEM_MIN_STARTS = 3
HIDDEN_GEM_MIN_COMPLETION = 0.5
LEAKY_MIN_STARTS = 5
LEAKY_MAX_COMPLETION = 0.35

SCHEDULED_WINDOW_DAYS = 7
# Failed background jobs have no "dismiss", so an old failure would otherwise
# sit in Needs attention forever.
FAILED_JOB_WINDOW_DAYS = 30

SESSION_EVENT_TYPES = [
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
]
KPI_EVENT_TYPES = [
    AnalyticsEvent.EVENT_STORY_STARTED,
    AnalyticsEvent.EVENT_STORY_COMPLETED,
    AnalyticsEvent.EVENT_AD_IMPRESSION,
    *SESSION_EVENT_TYPES,
]

# "Who" an event belongs to, as a SQL expression: the signed-in user when there
# is one, else the browser's visitor_id. The same rule as _content_identity()
# in analytics_api, so a person on two devices counts once and an anonymous
# browser counts once.
IDENTITY = Case(
    When(user__isnull=False, then=Concat(Value("u:"), Cast("user_id", CharField()))),
    default=Concat(Value("v:"), "visitor_id"),
    output_field=CharField(),
)


def get_overview_range_days(request):
    """Same allowed values as Analytics, but anything missing or invalid falls
    back to the Overview's own default (7) rather than Analytics' 30."""
    raw = str(request.query_params.get("days", ""))
    valid = raw == "all" or (raw.isdigit() and int(raw) in ALLOWED_RANGE_DAYS)
    return get_range_days(request) if valid else OVERVIEW_DEFAULT_RANGE_DAYS


def _windows(days, now):
    """(current, previous) as (start, end) pairs; previous is None for all-time."""
    if days == "all":
        return (None, None), None
    start = now - timedelta(days=days)
    return (start, None), (start - timedelta(days=days), start)


def _in(queryset, window, field="created_at"):
    return filter_since(queryset, window[0], field, until=window[1])


def _kpi(value, previous, sparkline):
    change_pct = None
    if previous:
        change_pct = round((value - previous) / previous * 100, 1)
    return {
        "value": value,
        "previous_value": previous,
        "change_pct": change_pct,
        "sparkline": sparkline,
    }


def _bucket_values(rows, days):
    """One number per bucket across the range, empty buckets included."""
    filled = fill_time_buckets(
        [{"day": day, "value": value} for day, value in rows.items()],
        days,
        defaults={"value": 0},
    )
    return [row["value"] for row in filled]


def _event_totals(events, window):
    totals = {event_type: {"count": 0} for event_type in KPI_EVENT_TYPES}
    for row in (
        _in(events.filter(event_type__in=KPI_EVENT_TYPES), window)
        .values("event_type")
        .annotate(count=Count("id"))
    ):
        totals[row["event_type"]] = {"count": row["count"]}
    return totals


def _capped_sessions(events, window, caps):
    """Every reading/listening/watching session in the window as
    (event_type, identity, created_at, capped_seconds). A session counts only
    up to SessionCaps, so a tab left open can't add hours of "engagement"."""
    rows = _in(events.filter(event_type__in=SESSION_EVENT_TYPES), window).values_list(
        "event_type", "user_id", "visitor_id", "duration_seconds", "metadata", "created_at", "story_id", "video_id"
    )
    for event_type, user_id, visitor_id, duration, metadata, created_at, story_id, video_id in rows:
        is_quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and (metadata or {}).get("format") == "quick_read"
        seconds = caps.capped(duration, event_type, story_id=story_id, video_id=video_id, is_quick_read=is_quick)
        yield event_type, ("u:%s" % user_id if user_id else "v:%s" % visitor_id), created_at, seconds


def _distinct_identities(events, window):
    return (
        _in(events, window)
        .annotate(identity=IDENTITY)
        .aggregate(n=Count("identity", distinct=True))["n"]
    )


def _signups(window):
    return _in(
        User.objects.filter(is_staff=False, is_superuser=False), window, "date_joined"
    )


def build_kpis(days, events, current, previous):
    visits = events.filter(event_type=AnalyticsEvent.EVENT_VISIT)
    current_totals = _event_totals(events, current)
    previous_totals = _event_totals(events, previous) if previous else None
    caps = SessionCaps()

    visitor_rows = {
        row["day"]: row["n"]
        for row in _in(visits, current)
        .annotate(identity=IDENTITY, day=time_trunc("created_at", days))
        .values("day")
        .annotate(n=Count("identity", distinct=True))
    }

    # One grouped query for every count-backed sparkline.
    counts = {}
    for row in (
        _in(events.filter(event_type__in=KPI_EVENT_TYPES), current)
        .annotate(day=time_trunc("created_at", days))
        .values("day", "event_type")
        .annotate(n=Count("id"))
    ):
        counts.setdefault(row["event_type"], {})[row["day"]] = row["n"]

    # Time spent is summed in Python so each session can be capped.
    seconds = {}
    for _event_type, _identity, created_at, capped in _capped_sessions(events, current, caps):
        bucket = python_time_bucket(created_at, days)
        seconds[bucket] = seconds.get(bucket, 0) + capped
    previous_seconds = (
        sum(capped for *_rest, capped in _capped_sessions(events, previous, caps)) if previous else None
    )

    signup_rows = {
        row["day"]: row["n"]
        for row in _signups(current)
        .annotate(day=time_trunc("date_joined", days))
        .values("day")
        .annotate(n=Count("id"))
    }

    def previous_of(getter):
        return getter(previous_totals) if previous_totals is not None else None

    started = AnalyticsEvent.EVENT_STORY_STARTED
    completed = AnalyticsEvent.EVENT_STORY_COMPLETED
    impressions = AnalyticsEvent.EVENT_AD_IMPRESSION

    return {
        "unique_visitors": _kpi(
            _distinct_identities(visits, current),
            _distinct_identities(visits, previous) if previous else None,
            _bucket_values(visitor_rows, days),
        ),
        "stories_started": _kpi(
            current_totals[started]["count"],
            previous_of(lambda t: t[started]["count"]),
            _bucket_values(counts.get(started, {}), days),
        ),
        "stories_completed": _kpi(
            current_totals[completed]["count"],
            previous_of(lambda t: t[completed]["count"]),
            _bucket_values(counts.get(completed, {}), days),
        ),
        "engagement_minutes": _kpi(
            round(sum(seconds.values()) / 60, 1),
            round(previous_seconds / 60, 1) if previous_seconds is not None else None,
            [round(value / 60, 1) for value in _bucket_values(seconds, days)],
        ),
        "new_signups": _kpi(
            _signups(current).count(),
            _signups(previous).count() if previous else None,
            _bucket_values(signup_rows, days),
        ),
        "ad_impressions": _kpi(
            current_totals[impressions]["count"],
            previous_of(lambda t: t[impressions]["count"]),
            _bucket_values(counts.get(impressions, {}), days),
        ),
    }, visitor_rows


def build_funnel(events, cutoff):
    lifecycle = build_story_lifecycle_counts(cutoff)
    visits = events.filter(event_type=AnalyticsEvent.EVENT_VISIT)
    steps = [
        ("visits", "Visits", filter_since(visits, cutoff).count(), None),
        ("story_opened", "Story opened", lifecycle["detail_views"], "of visits"),
        ("started", "Started reading", lifecycle["starts"], "of stories opened"),
        ("completed", "Completed", lifecycle["completions"], "of stories started"),
    ]
    funnel = []
    for index, (key, label, count, rate_label) in enumerate(steps):
        funnel.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "rate": _rate(count, steps[index - 1][2]) if index else None,
                "rate_label": rate_label,
            }
        )
    return funnel


def _average(values):
    return round(sum(values) / len(values), 3) if values else None


def _story_completion_average(progress_qs, unit_counts, cutoff):
    """Mean share of a story finished, over (reader, story) pairs active in
    range. Progress rows are per chapter / per audio track, so a pair's share
    is the sum of its rows over how many units the story has — listening is
    therefore per story, not per track."""
    pairs = (
        audience_only(progress_qs)
        .values("user_id", "story_id")
        .annotate(total=Sum("progress"), last=Max("updated_at"))
    )
    if cutoff is not None:
        pairs = pairs.filter(last__gte=cutoff)
    shares = [
        min(1.0, row["total"] / unit_counts[row["story_id"]])
        for row in pairs
        if unit_counts.get(row["story_id"])
    ]
    return _average(shares)


def build_formats(events, cutoff):
    caps = SessionCaps()
    seconds, people = {}, {}
    for event_type, identity, _created_at, capped in _capped_sessions(events, (cutoff, None), caps):
        seconds[event_type] = seconds.get(event_type, 0) + capped
        people.setdefault(event_type, set()).add(identity)

    def card(event_type, avg_completion):
        return {
            "minutes": round(seconds.get(event_type, 0) / 60, 1),
            "unique_users": len(people.get(event_type, ())),
            "avg_completion": avg_completion,
        }

    chapter_counts = dict(Chapter.objects.values_list("story_id").annotate(n=Count("id")))
    audio_counts = dict(Audio.objects.values_list("story_id").annotate(n=Count("id")))
    watch_avg = filter_since(
        audience_only(VideoWatchProgress.objects.all()), cutoff, "updated_at"
    ).aggregate(avg=Avg("progress"))["avg"]

    return {
        "read": card(
            AnalyticsEvent.EVENT_READING_SESSION,
            _story_completion_average(ChapterReadingProgress.objects.all(), chapter_counts, cutoff),
        ),
        "listen": card(
            AnalyticsEvent.EVENT_LISTENING_SESSION,
            _story_completion_average(AudioReadingProgress.objects.all(), audio_counts, cutoff),
        ),
        "watch": card(
            AnalyticsEvent.EVENT_WATCHING_SESSION,
            round(watch_avg, 3) if watch_avg is not None else None,
        ),
    }


def build_movers(days, events, current, previous):
    now_rows = build_content_rankings(days, "story")
    ranked = [row for row in sort_content_rankings(now_rows, "performance_score") if row["performance_score"] > 0]

    previous_rank = {}
    if previous:
        previous_rows = build_content_rankings(days, "story", until=previous[1])
        previous_rank = {
            row["id"]: position
            for position, row in enumerate(
                (r for r in sort_content_rankings(previous_rows, "performance_score") if r["performance_score"] > 0),
                start=1,
            )
        }

    # Per-story starts and finishes: the rankings don't carry a start count,
    # and completion rate is what separates a gem from a leak.
    lifecycle = {}
    for row in (
        _in(
            events.filter(
                event_type__in=[AnalyticsEvent.EVENT_STORY_STARTED, AnalyticsEvent.EVENT_STORY_COMPLETED],
                story_id__isnull=False,
            ),
            current,
        )
        .values("story_id", "event_type")
        .annotate(n=Count("id"))
    ):
        entry = lifecycle.setdefault(row["story_id"], {"starts": 0, "completions": 0})
        key = "starts" if row["event_type"] == AnalyticsEvent.EVENT_STORY_STARTED else "completions"
        entry[key] = row["n"]

    def shape(row, **extra):
        stats = lifecycle.get(row["id"], {"starts": 0, "completions": 0})
        starts = stats["starts"]
        return {
            "id": row["id"],
            "title": row["title"],
            "slug": row["slug"],
            "performance_score": row["performance_score"],
            "views": row["views"],
            "starts": starts,
            "completions": stats["completions"],
            # A finish can belong to a start from before the window, so cap it.
            "completion_rate": min(1.0, round(stats["completions"] / starts, 3)) if starts else None,
            **extra,
        }

    trending = [
        shape(
            row,
            rank_change=(previous_rank[row["id"]] - position) if row["id"] in previous_rank else None,
        )
        for position, row in enumerate(ranked[:MOVERS_LIMIT], start=1)
    ]

    active_views = [row["views"] for row in now_rows if row["views"] or row["id"] in lifecycle]
    median_views = statistics.median(active_views) if active_views else 0
    shaped = [shape(row) for row in now_rows if row["id"] in lifecycle]

    hidden_gems = sorted(
        (
            row
            for row in shaped
            if row["starts"] >= HIDDEN_GEM_MIN_STARTS
            and row["completion_rate"] >= HIDDEN_GEM_MIN_COMPLETION
            and row["views"] < median_views
        ),
        key=lambda row: (-row["completion_rate"], -row["starts"], row["title"].lower()),
    )[:MOVERS_LIMIT]
    leaky = sorted(
        (
            row
            for row in shaped
            if row["starts"] >= LEAKY_MIN_STARTS and row["completion_rate"] < LEAKY_MAX_COMPLETION
        ),
        # Biggest absolute loss first: 40 readers lost beats 5 of 6.
        key=lambda row: (-(row["starts"] - row["completions"]), row["title"].lower()),
    )[:MOVERS_LIMIT]

    return {"trending": trending, "hidden_gems": hidden_gems, "leaky": leaky}


def build_sources(events, cutoff):
    visits = filter_since(events.filter(event_type=AnalyticsEvent.EVENT_VISIT), cutoff)
    countries = (
        visits.exclude(country_code="")
        .annotate(identity=IDENTITY)
        .values("country_code")
        .annotate(visitors=Count("identity", distinct=True))
        .order_by("-visitors", "country_code")[:3]
    )
    return {
        "referrers": build_referral_sources(visits)[:3],
        "countries": [{"country_code": row["country_code"], "visitors": row["visitors"]} for row in countries],
    }


def build_overview_data(days):
    """Everything derived from analytics events — the expensive part, cached."""
    now = timezone.now()
    cutoff = get_cutoff(days)
    current, previous = _windows(days, now)
    events = audience_events(AnalyticsEvent.objects.all())

    kpis, visitor_rows = build_kpis(days, events, current, previous)
    return {
        "range_days": days,
        "time_interval": get_time_interval(days),
        "kpis": kpis,
        "traffic": {
            "series": [
                {"day": row["day"], "visitors": row["value"]}
                for row in fill_time_buckets(
                    [{"day": day, "value": n} for day, n in visitor_rows.items()],
                    days,
                    defaults={"value": 0},
                )
            ],
            "publish_markers": build_publish_markers(days, cutoff),
        },
        "funnel": build_funnel(events, cutoff),
        "formats": build_formats(events, cutoff),
        "movers": build_movers(days, events, current, previous),
        "sources": build_sources(events, cutoff),
    }


def build_needs_attention():
    """Things an admin should go and do something about. Only items with a
    non-zero count are returned. Cheap counts, computed live on every request
    so clearing an item shows up immediately."""
    now = timezone.now()
    failed = Story.GEN_STATUS_FAILED
    published = published_story_q()
    no_cover = (Q(cover_image_file="") | Q(cover_image_file__isnull=True)) & (
        Q(cover_image="") | Q(cover_image__isnull=True)
    )
    # distinct=True throughout: the audios join would otherwise repeat a story
    # once per track in every count.
    story_counts = Story.objects.aggregate(
        failed_ai=Count(
            "id", filter=Q(summary_status=failed) | Q(retrospective_status=failed), distinct=True
        ),
        scheduled=Count(
            "id",
            filter=Q(
                is_published=True,
                publish_at__gt=now,
                publish_at__lte=now + timedelta(days=SCHEDULED_WINDOW_DAYS),
            ),
            distinct=True,
        ),
        missing_cover=Count("id", filter=published & no_cover, distinct=True),
        missing_audio=Count("id", filter=published & Q(audios__isnull=True), distinct=True),
    )
    job_cutoff = now - timedelta(days=FAILED_JOB_WINDOW_DAYS)
    candidates = [
        ("feedback", "New feedback", Feedback.objects.filter(status=Feedback.STATUS_NEW).count(), "/admin/feedback"),
        (
            "submissions",
            "Pending submissions",
            Submission.objects.filter(status="pending").count(),
            "/admin/submissions",
        ),
        (
            "failed_ai",
            "Stories with a failed AI summary or retrospective",
            story_counts["failed_ai"],
            "/admin/story-report",
        ),
        (
            "failed_epub_imports",
            "Failed EPUB imports",
            EpubImportJob.objects.filter(status=EpubImportJob.STATUS_FAILED, created_at__gte=job_cutoff).count(),
            "/admin/content",
        ),
        (
            "failed_book_fetches",
            "Failed book fetches",
            BookFetchJob.objects.filter(status=BookFetchJob.STATUS_FAILED, created_at__gte=job_cutoff).count(),
            "/admin/content",
        ),
        ("scheduled", "Stories going live this week", story_counts["scheduled"], "/admin/content"),
        ("missing_cover", "Published stories missing a cover", story_counts["missing_cover"], "/admin/content"),
        ("missing_audio", "Published stories with no audio", story_counts["missing_audio"], "/admin/content"),
        (
            "queue_backlog",
            "Story queue backlog",
            StoryQueue.objects.filter(is_added=False).count(),
            "/admin/content",
        ),
    ]
    return [
        {"key": key, "label": label, "count": count, "link": link}
        for key, label, count, link in candidates
        if count > 0
    ]


def build_catalog():
    return {
        "stories": Story.objects.count(),
        "chapters": Chapter.objects.count(),
        "audios": Audio.objects.count(),
        "videos": Video.objects.count(),
        "users": User.objects.count(),
    }


class AdminOverviewAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        days = get_overview_range_days(request)
        key = OVERVIEW_CACHE_KEY.format(days=days)
        data = cache.get(key)
        if data is None:
            data = build_overview_data(days)
            cache.set(key, data, CACHE_SECONDS)
        # Needs-attention and the catalogue totals sit outside the cache: they
        # are a handful of counts, and a stale "3 pending submissions" right
        # after approving them would just look broken.
        return Response({**data, "needs_attention": build_needs_attention(), "catalog": build_catalog()})

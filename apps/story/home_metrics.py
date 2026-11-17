"""Numbers the homepage shows about the catalogue and its audience.

Two things live here, both cached because the homepage is the busiest page:

* `get_home_stats()` — the hero stat strip. Replaces the old lifetime-views
  "readers" and Author-count "creators" with figures a visitor can take at
  face value: unique people who read/listened/watched in the last 30 days,
  countries and languages the catalogue covers, hours of audiobooks.
* `trending_story_ids()` — what the `trending` story sort ranks by: unique
  audience readers/listeners over the last 7 days.

Analytics helpers are imported lazily: analytics_api imports `.api`, which
imports the home-section module that uses this one.
"""

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.stats.models import AnalyticsEvent

from .models import Audio, Author, Story

HOME_STATS_CACHE_KEY = "home:stats:v1"
HOME_STATS_CACHE_SECONDS = 60 * 60
TRENDING_CACHE_KEY = "home:trending-ids:v1"
TRENDING_CACHE_SECONDS = 60 * 15

TRENDING_WINDOW_DAYS = 7
TRENDING_FALLBACK_DAYS = 30
# Fewer stories than this with any activity in the short window means the
# ranking would be noise, so the longer window is used instead.
TRENDING_MIN_STORIES = 5
TRENDING_MAX_IDS = 200

# Events that mean someone actually engaged with a story, as opposed to a page
# view: starting it, or spending time reading / listening / watching.
ENGAGEMENT_EVENTS = (
    AnalyticsEvent.EVENT_STORY_STARTED,
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
    AnalyticsEvent.EVENT_QUICK_READ_OPENED,
)


def _engagement_events(days):
    from .analytics_api import audience_events

    since = timezone.now() - timedelta(days=days)
    return audience_events(
        AnalyticsEvent.objects.filter(event_type__in=ENGAGEMENT_EVENTS, created_at__gte=since)
    )


def unique_readers(days=30):
    """Distinct people (the user when signed in, else the browser) who engaged
    with a story in the window. Staff and suspected bots are excluded."""
    from .analytics_api import _content_identity

    rows = _engagement_events(days).filter(story__isnull=False).values_list("user_id", "visitor_id").distinct()
    return len({_content_identity(user_id, visitor_id) for user_id, visitor_id in rows})


def _compute_home_stats():
    published = Story.objects.published()
    audio_seconds = (
        Audio.objects.filter(story__in=published).aggregate(total=Sum("duration_seconds"))["total"] or 0
    )
    return {
        "stories": published.count(),
        "readers_30d": unique_readers(30),
        "countries": published.exclude(country="").values("country").distinct().count(),
        "languages": published.exclude(language="").values("language").distinct().count(),
        "audio_hours": round(audio_seconds / 3600),
        # Legacy keys for hero templates saved before the honest stats existed.
        "creators": Author.objects.count(),
        "readers": published.aggregate(total=Sum("views"))["total"] or 0,
    }


def get_home_stats():
    stats = cache.get(HOME_STATS_CACHE_KEY)
    if stats is None:
        stats = _compute_home_stats()
        cache.set(HOME_STATS_CACHE_KEY, stats, HOME_STATS_CACHE_SECONDS)
    return stats


def _trending_in_window(days):
    published_ids = Story.objects.published().values("pk")
    rows = (
        _engagement_events(days)
        .filter(story__in=published_ids)
        .values("story_id")
        .annotate(
            signed_in=Count("user_id", distinct=True),
            anonymous=Count("visitor_id", filter=Q(user__isnull=True), distinct=True),
        )
    )
    scored = [(row["signed_in"] + row["anonymous"], row["story_id"]) for row in rows]
    scored.sort(key=lambda pair: (-pair[0], -pair[1]))
    return [story_id for _readers, story_id in scored]


def trending_story_ids():
    """Story ids ranked by unique recent readers/listeners, best first."""
    ids = cache.get(TRENDING_CACHE_KEY)
    if ids is None:
        ids = _trending_in_window(TRENDING_WINDOW_DAYS)
        if len(ids) < TRENDING_MIN_STORIES:
            ids = _trending_in_window(TRENDING_FALLBACK_DAYS)
        ids = ids[:TRENDING_MAX_IDS]
        cache.set(TRENDING_CACHE_KEY, ids, TRENDING_CACHE_SECONDS)
    return ids


def section_click_stats(windows=(7, 30)):
    """Per-section homepage clicks and click-through for the admin list.

    Click-through is clicks ÷ homepage visits (visit events landing on "/")
    in the same window — a rough rate, since one visit can click several
    sections, but comparable between sections. Audience traffic only.
    Returns {"visits": {days: n}, "sections": {section_id: {days: {...}}}}.
    """
    from .analytics_api import audience_events

    now = timezone.now()
    result = {"windows": list(windows), "visits": {}, "sections": {}}
    for days in windows:
        since = now - timedelta(days=days)
        visits = audience_events(
            AnalyticsEvent.objects.filter(
                event_type=AnalyticsEvent.EVENT_VISIT, metadata__path="/", created_at__gte=since
            )
        ).count()
        result["visits"][str(days)] = visits
        clicks = (
            audience_events(
                AnalyticsEvent.objects.filter(
                    event_type=AnalyticsEvent.EVENT_HOME_SECTION_CLICK, created_at__gte=since
                )
            )
            .values("metadata__section_id")
            .annotate(total=Count("id"))
        )
        for row in clicks:
            try:
                section_id = int(row["metadata__section_id"])
            except (TypeError, ValueError):
                continue
            entry = result["sections"].setdefault(str(section_id), {})
            entry[str(days)] = {
                "clicks": row["total"],
                "click_through": round(row["total"] / visits, 4) if visits else None,
            }
    return result

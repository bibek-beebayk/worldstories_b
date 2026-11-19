"""Discover page data.

The page is "I don't know what I want", so every list here answers a question a
reader can act on — what are people reading right now, what do few people know
but finish, what just arrived, where in the world is there to read. Replaces the
old leaderboards, which ranked on the lifetime `Story.views` counter and on a
handful of reviews and favourites.

Audience numbers follow the Analytics rules: staff and suspected bots are out,
and a "reader" is the signed-in user, else the browser (`_content_identity`).
Analytics helpers are imported lazily: analytics_api imports `.api`.
"""

import re
import statistics
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Count, Max, Q
from django.utils import timezone
from django.utils.html import strip_tags

from apps.stats.models import AnalyticsEvent

from .filters import apply_format
from .home_metrics import ENGAGEMENT_EVENTS
from .home_sections import missing_cover_q
from .library_api import COUNTRY_NAMES, _covers
from .models import Genre, Review, Story, Video, published_story_q

TRENDING_LIMIT = 10
TRENDING_WINDOW_DAYS = 7
TRENDING_FALLBACK_DAYS = 30
# Fewer readers than this on the top story and the week is too quiet to rank.
TRENDING_MIN_TOP_READERS = 5
TRENDING_CACHE_KEY = "discover:trending:v1"
TRENDING_CACHE_SECONDS = 60 * 15

GEMS_WINDOW_DAYS = 90
GEMS_MIN_STARTERS = 5
GEMS_REVIEW_MIN_COUNT = 3
GEMS_REVIEW_MIN_AVERAGE = 4.5
GEMS_LIMIT = 6
GEMS_CACHE_KEY = "discover:hidden-gems:v1"
GEMS_CACHE_SECONDS = 60 * 60

JUST_ADDED_LIMIT = 24
COUNTRY_TILES = 8
GENRE_TILES = 12

BLURB_LENGTH = 180

# When two formats of one story share a date (audio, read-along and summaries
# carry no creation date of their own, so they inherit the publish date), the
# richer, later-added format wins: text exists from the moment of publishing,
# the rest is added afterwards.
FORMAT_PRIORITY = ("watch", "read_along", "listen", "quick_read", "read")


def _identity(user_id, visitor_id):
    from .analytics_api import _content_identity

    return _content_identity(user_id, visitor_id)


def _window_events(start, end=None, event_types=ENGAGEMENT_EVENTS):
    from .analytics_api import audience_events

    queryset = AnalyticsEvent.objects.filter(event_type__in=event_types, created_at__gte=start, story__isnull=False)
    if end is not None:
        queryset = queryset.filter(created_at__lt=end)
    return audience_events(queryset)


# --- trending ----------------------------------------------------------------


def readers_by_story(days, *, offset_days=0):
    """{story_id: unique readers/listeners/watchers} for a window of `days`
    ending `offset_days` ago. Published stories only."""
    end = timezone.now() - timedelta(days=offset_days)
    start = end - timedelta(days=days)
    published = set(Story.objects.published().values_list("pk", flat=True))
    people = defaultdict(set)
    for story_id, user_id, visitor_id in _window_events(start, end).values_list("story_id", "user_id", "visitor_id"):
        if story_id in published:
            people[story_id].add(_identity(user_id, visitor_id))
    return {story_id: len(ids) for story_id, ids in people.items()}


def _ranked(counts):
    return sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))


def compute_trending():
    days = TRENDING_WINDOW_DAYS
    current = _ranked(readers_by_story(days))
    if not current or current[0][1] < TRENDING_MIN_TOP_READERS:
        days = TRENDING_FALLBACK_DAYS
        current = _ranked(readers_by_story(days))
    previous_rank = {story_id: index for index, (story_id, _n) in enumerate(_ranked(readers_by_story(days, offset_days=days)))}
    rows = []
    for index, (story_id, readers) in enumerate(current[:TRENDING_LIMIT]):
        before = previous_rank.get(story_id)
        rows.append(
            {
                "story_id": story_id,
                "readers": readers,
                "window_days": days,
                # Places gained since the window before (positive = moved up);
                # null when the story was not ranked then.
                "rank_change": None if before is None else before - index,
            }
        )
    return rows


def get_trending():
    rows = cache.get(TRENDING_CACHE_KEY)
    if rows is None:
        rows = compute_trending()
        cache.set(TRENDING_CACHE_KEY, rows, TRENDING_CACHE_SECONDS)
    return rows


# --- hidden gems -------------------------------------------------------------


def compute_hidden_gems():
    """Stories few people have found that those who start them finish.

    A gem has at least GEMS_MIN_STARTERS unique starters in the last 90 days and
    fewer unique viewers than the median story that anyone viewed at all, and is
    ranked by completion rate (readers who finished ÷ readers who started). A
    story with 3+ reviews averaging 4.5★ or better also qualifies, whatever its
    traffic — it is a gem on the readers' own word.
    """
    from .analytics_api import audience_only

    start = timezone.now() - timedelta(days=GEMS_WINDOW_DAYS)
    published = set(Story.objects.published().values_list("pk", flat=True))
    types = (AnalyticsEvent.EVENT_VISIT, AnalyticsEvent.EVENT_STORY_COMPLETED, AnalyticsEvent.EVENT_STORY_STARTED, *ENGAGEMENT_EVENTS)
    viewers, starters, finishers = defaultdict(set), defaultdict(set), defaultdict(set)
    for event_type, story_id, user_id, visitor_id in _window_events(start, event_types=set(types)).values_list(
        "event_type", "story_id", "user_id", "visitor_id"
    ):
        if story_id not in published:
            continue
        identity = _identity(user_id, visitor_id)
        if event_type == AnalyticsEvent.EVENT_STORY_COMPLETED:
            finishers[story_id].add(identity)
            continue
        viewers[story_id].add(identity)
        if event_type == AnalyticsEvent.EVENT_STORY_STARTED:
            starters[story_id].add(identity)

    gems = {}
    if viewers:
        median_viewers = statistics.median(len(ids) for ids in viewers.values())
        for story_id, began in starters.items():
            if len(began) < GEMS_MIN_STARTERS or len(viewers[story_id]) >= median_viewers:
                continue
            rate = min(1.0, len(finishers.get(story_id, ())) / len(began))
            gems[story_id] = {"strength": rate, "starters": len(began), "reason": f"{round(rate * 100)}% of readers finish it"}

    reviewed = (
        audience_only(Review.objects.filter(story_id__in=published))
        .values("story_id")
        .annotate(n=Count("id"), avg=Avg("rating"))
        .filter(n__gte=GEMS_REVIEW_MIN_COUNT, avg__gte=GEMS_REVIEW_MIN_AVERAGE)
    )
    for row in reviewed:
        if row["story_id"] in gems:
            continue  # already in, with the completion reason
        gems[row["story_id"]] = {
            "strength": row["avg"] / 5,
            "starters": 0,
            "reason": f"{row['avg']:.1f}★ from {row['n']} readers",
        }

    ranked = sorted(gems.items(), key=lambda kv: (-kv[1]["strength"], -kv[1]["starters"], kv[0]))[:GEMS_LIMIT]
    return [{"story_id": story_id, "reason": gem["reason"]} for story_id, gem in ranked]


def get_hidden_gems():
    rows = cache.get(GEMS_CACHE_KEY)
    if rows is None:
        rows = compute_hidden_gems()
        cache.set(GEMS_CACHE_KEY, rows, GEMS_CACHE_SECONDS)
    return rows


# --- just added ----------------------------------------------------------------


def _as_datetime(value):
    if value is None:
        return None
    if hasattr(value, "hour"):
        return value
    from datetime import datetime, time

    return timezone.make_aware(datetime.combine(value, time.min))


def compute_just_added(limit=JUST_ADDED_LIMIT):
    """[(story_id, format)] — each story once, under its newest format, newest first."""
    published = Story.objects.published()
    published_ids = published.values("pk")
    pool = limit * 3  # enough per format that the merge still has `limit` after de-duplication
    candidates = defaultdict(list)  # story_id -> [(when, format)]

    def add(fmt, rows):
        for story_id, when in rows:
            when = _as_datetime(when)
            if when is not None:
                candidates[story_id].append((when, fmt))

    for fmt in ("read", "listen", "read_along", "quick_read"):
        qs = apply_format(published, fmt).exclude(site_published_date__isnull=True)
        add(fmt, qs.order_by("-site_published_date", "-id").values_list("pk", "site_published_date")[:pool])
    add(
        "watch",
        Video.objects.filter(story_id__in=published_ids)
        .values_list("story_id")
        .annotate(latest=Max("created_at"))
        .order_by("-latest")[:pool],
    )

    best = {}
    for story_id, options in candidates.items():
        # newest first; on a tie, the richer format
        options.sort(key=lambda option: (option[0], -FORMAT_PRIORITY.index(option[1])), reverse=True)
        best[story_id] = options[0]
    ranked = sorted(best.items(), key=lambda kv: (kv[1][0], -kv[0]), reverse=True)[:limit]
    return [(story_id, fmt) for story_id, (_when, fmt) in ranked]


# --- countries / genres ----------------------------------------------------------


def compute_countries(request):
    """The countries with the most published stories. Country is a story-level
    field (Story.country, an ISO code) — authors have none."""
    published = Story.objects.published()
    counts = (
        published.exclude(country="")
        .values("country")
        .annotate(n=Count("pk"))
        .order_by("-n", "country")[:COUNTRY_TILES]
    )
    return [
        {
            "code": row["country"],
            "name": COUNTRY_NAMES.get(row["country"], row["country"]),
            "count": row["n"],
            "covers": _covers(published.filter(country=row["country"]), request),
        }
        for row in counts
    ]


def compute_popular_genres():
    genres = (
        Genre.objects.annotate(n=Count("stories", filter=published_story_q("stories"), distinct=True))
        .filter(n__gt=0)
        .order_by("-n", "name")[:GENRE_TILES]
    )
    return [{"id": g.pk, "name": g.name, "slug": g.slug or str(g.pk), "count": g.n} for g in genres]


# --- surprise -------------------------------------------------------------------


def blurb_for(story):
    """One plain-text line from the story's description, else its summary."""
    for raw in (story.about, story.summary):
        text = re.sub(r"\s+", " ", strip_tags(raw or "")).strip()
        if text:
            return text if len(text) <= BLURB_LENGTH else text[: BLURB_LENGTH - 1].rstrip() + "…"
    return ""


def surprise_pool(exclude_ids=()):
    """Published stories that have a real cover and something to say about them."""
    has_blurb = (Q(about__isnull=False) & ~Q(about="")) | (Q(summary__isnull=False) & ~Q(summary=""))
    qs = Story.objects.published().exclude(missing_cover_q()).filter(has_blurb)
    return qs.exclude(pk__in=list(exclude_ids)) if exclude_ids else qs

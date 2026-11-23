"""Resolution and validation of the admin-managed homepage sections.

`resolve_home_sections` turns the enabled `HomeSection` rows into the public
`sections` array served by /api/home/: content sections carry their already
resolved items (so the SSR loader can render them on first paint), widget
sections carry only their presentation settings.
"""

import random
from datetime import timedelta
from statistics import median
from urllib.parse import urlencode

from django.core.cache import cache
from django.db.models import Case, Count, F, IntegerField, Prefetch, Q, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from .filters import public_story_mood_q
from .models import (
    HOME_SECTION_ICON_NAMES,
    COUNTRY_CHOICES,
    Audio,
    Blog,
    HomeSection,
    Story,
    Video,
    published_story_q,
    with_preferred_translation_only,
)

# Extra items fetched beyond `item_limit` when repeats are not allowed: the
# frontend drops stories already shown higher up the page (it alone knows the
# reader's personal shelves) and then trims back to `item_limit`.
DEDUPE_BUFFER = 12
MAX_ITEMS = 24

STORY_SORTS = {
    "newest": ("-site_published_date", "-id"),
    "recently_added": ("-id",),
    "popular": ("-views", "-rating", "-id"),
    "top_rated": ("-rating", "-views", "-id"),
    # Not a column ordering: ranked by unique recent audience readers (see
    # home_metrics.trending_story_ids). The tuple is only the tie-break
    # fallback and what a non-special-cased caller would get.
    "trending": ("-id",),
    "oldest": ("site_published_date", "id"),
    "az": ("title", "id"),
    "shortest": (),
    "longest": (),
    "most_completed": (),
    "hidden_gems": (),
    "daily_shuffle": (),
}
TRENDING_SORT = "trending"
HIDDEN_GEMS_MEDIAN_CACHE_KEY = "home:published-story-median-views:v1"
HIDDEN_GEMS_MEDIAN_CACHE_SECONDS = 60 * 10
STORY_SORT_OPTIONS = [
    {"value": "newest", "label": "Newest", "description": "Most recently published first."},
    {"value": "recently_added", "label": "Recently added", "description": "Most recently added to WorldStories first."},
    {"value": "popular", "label": "Popular", "description": "Most viewed stories first."},
    {"value": "top_rated", "label": "Top rated", "description": "Highest-rated stories first."},
    {"value": "trending", "label": "Trending", "description": "Most unique readers in the selected recent window."},
    {"value": "oldest", "label": "Oldest", "description": "Earliest site publication date first."},
    {"value": "az", "label": "A to Z", "description": "Story title in alphabetical order."},
    {"value": "shortest", "label": "Shortest", "description": "Shortest estimated reading time first."},
    {"value": "longest", "label": "Longest", "description": "Longest estimated reading time first."},
    {"value": "most_completed", "label": "Most completed", "description": "Stories completed by the most readers first."},
    {"value": "hidden_gems", "label": "Hidden gems", "description": "Highly rated stories with below-median catalogue views."},
    {"value": "daily_shuffle", "label": "Daily shuffle", "description": "A stable order that changes each day and differs by section."},
]
VIDEO_SORT_OPTIONS = [
    {"value": "newest", "label": "Newest", "description": "Most recently added videos first."},
    {"value": "oldest", "label": "Oldest", "description": "Earliest-added videos first."},
    {"value": "longest", "label": "Longest", "description": "Longest videos first; unknown durations appear last."},
    {"value": "shortest", "label": "Shortest", "description": "Shortest videos first; unknown durations appear last."},
    {"value": "daily_shuffle", "label": "Daily shuffle", "description": "A stable order that changes each day and differs by section."},
]
BLOG_SORT_OPTIONS = [
    {"value": "newest", "label": "Newest", "description": "Most recently published posts first."},
    {"value": "oldest", "label": "Oldest", "description": "Earliest-published posts first."},
    {"value": "shortest", "label": "Shortest", "description": "Shortest estimated reading time first."},
    {"value": "daily_shuffle", "label": "Daily shuffle", "description": "A stable order that changes each day and differs by section."},
]
VIDEO_SORTS = {option["value"] for option in VIDEO_SORT_OPTIONS}
BLOG_SORTS = {option["value"] for option in BLOG_SORT_OPTIONS}
BANNER_THEMES = ["ocean", "violet", "rose", "amber", "emerald"]
AD_SIZES = ["banner", "square", "rectangle"]

# What the admin form offers per type. Served by the `types` endpoint so the
# form needs no per-type knowledge of its own beyond rendering these fields.
TYPE_META = {
    HomeSection.TYPE_STORIES_QUERY: {"content": True, "fields": ["layout", "item_limit", "card_style", "allow_repeats", "see_all", "icon", "variant", "filters", "countries", "authors", "exclude_tags", "exclude_story_ids", "pin_story_ids", "min_reading_minutes", "max_reading_minutes", "published_within_days", "min_rating", "min_ratings_count", "trending_days"]},
    HomeSection.TYPE_STORIES_MANUAL: {"content": True, "fields": ["layout", "item_limit", "card_style", "allow_repeats", "see_all", "icon", "variant", "story_ids"]},
    HomeSection.TYPE_VIDEOS: {"content": True, "fields": ["layout", "item_limit", "see_all", "icon", "video_filters", "video_sort", "format", "min_duration_seconds", "max_duration_seconds", "language", "story_genres", "video_ids"]},
    HomeSection.TYPE_BLOGS: {"content": True, "fields": ["layout", "item_limit", "see_all", "icon", "blog_sort", "topics", "published_within_days", "story_genres", "only_with_story", "pin_blog_ids", "blog_ids"]},
    HomeSection.TYPE_BANNER: {"content": False, "fields": ["icon", "banner"]},
    HomeSection.TYPE_AD_SLOT: {"content": False, "fields": ["ad_size"]},
    HomeSection.TYPE_NEW_ENTRIES: {"content": False, "fields": ["icon"]},
    HomeSection.TYPE_CONTINUE_SHELF: {"content": False, "fields": []},
    HomeSection.TYPE_READING_JOURNEY: {"content": False, "fields": []},
    HomeSection.TYPE_RECOMMENDED: {"content": False, "fields": ["see_all", "icon"]},
    HomeSection.TYPE_JOURNEYS_BANNER: {"content": False, "fields": []},
    HomeSection.TYPE_EXPLORE_TILES: {"content": False, "fields": []},
}


def _int_list(value, name, max_len=50):
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > max_len:
        raise ValueError(f"{name} must be a list of up to {max_len} ids.")
    out = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must contain only ids.")
        if number not in out:
            out.append(number)
    return out


def _optional_int(value, name):
    if value in (None, "", "all"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an id.")


def _text(value, name, max_len):
    value = "" if value is None else str(value).strip()
    if len(value) > max_len:
        raise ValueError(f"{name} is too long (max {max_len}).")
    return value


def _bounded_int(value, name, minimum, maximum):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    if number < minimum or number > maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return number


def _bounded_float(value, name, minimum, maximum):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number from {minimum} to {maximum}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number from {minimum} to {maximum}.")
    if number < minimum or number > maximum:
        raise ValueError(f"{name} must be a number from {minimum} to {maximum}.")
    return number


def _nonnegative_int(value, name):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative integer.")
    if number < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return number


def _countries(value):
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("countries must be a list of up to 30 country codes.")
    valid = {code for code, _label in COUNTRY_CHOICES}
    result = []
    for item in value:
        code = item.strip().upper() if isinstance(item, str) else ""
        if code not in valid:
            raise ValueError(f"countries contains an unknown country code: {item}.")
        if code not in result:
            result.append(code)
    return result


def clean_config(section_type, config):
    """Whitelist + validate a section's `config` for its type. Unknown keys are
    dropped, so stale settings never linger after a type change. Raises
    ValueError with a user-facing message."""
    config = config if isinstance(config, dict) else {}
    if section_type == HomeSection.TYPE_STORIES_QUERY:
        sort = config.get("sort") or "newest"
        if sort not in STORY_SORTS:
            raise ValueError(f"sort must be one of: {', '.join(STORY_SORTS)}.")
        moods = config.get("moods") or []
        if not isinstance(moods, list) or not all(isinstance(m, str) for m in moods):
            raise ValueError("moods must be a list of mood slugs.")
        min_reading = _bounded_int(config.get("min_reading_minutes"), "min_reading_minutes", 1, 600)
        max_reading = _bounded_int(config.get("max_reading_minutes"), "max_reading_minutes", 1, 600)
        if min_reading is not None and max_reading is not None and min_reading > max_reading:
            raise ValueError("min_reading_minutes cannot be greater than max_reading_minutes.")
        trending_days = _bounded_int(config.get("trending_days", 7), "trending_days", 7, 30)
        if trending_days not in (7, 30):
            raise ValueError("trending_days must be 7 or 30.")
        return {
            "sort": sort,
            "genres": _int_list(config.get("genres"), "genres"),
            "categories": _int_list(config.get("categories"), "categories"),
            "tags": _int_list(config.get("tags"), "tags"),
            "themes": _int_list(config.get("themes"), "themes"),
            "moods": [m.strip() for m in moods if m.strip()][:20],
            "story_type": _optional_int(config.get("story_type"), "story_type"),
            "language": _text(config.get("language"), "language", 10),
            "original_only": bool(config.get("original_only")),
            "has_summary": bool(config.get("has_summary")),
            "has_audio": bool(config.get("has_audio")),
            "has_video": bool(config.get("has_video")),
            "has_read_along": bool(config.get("has_read_along")),
            "require_cover": bool(config.get("require_cover")),
            "countries": _countries(config.get("countries")),
            "authors": _int_list(config.get("authors"), "authors"),
            "exclude_tags": _int_list(config.get("exclude_tags"), "exclude_tags"),
            "exclude_story_ids": _int_list(config.get("exclude_story_ids"), "exclude_story_ids", max_len=50),
            "pin_story_ids": _int_list(config.get("pin_story_ids"), "pin_story_ids", max_len=12),
            "min_reading_minutes": min_reading,
            "max_reading_minutes": max_reading,
            "published_within_days": _bounded_int(config.get("published_within_days"), "published_within_days", 1, 3650),
            "min_rating": _bounded_float(config.get("min_rating"), "min_rating", 0, 5),
            "min_ratings_count": _bounded_int(config.get("min_ratings_count"), "min_ratings_count", 0, 1000),
            "trending_days": trending_days,
        }
    if section_type == HomeSection.TYPE_STORIES_MANUAL:
        return {"story_ids": _int_list(config.get("story_ids"), "story_ids", max_len=48)}
    if section_type == HomeSection.TYPE_VIDEOS:
        video_sort = config.get("video_sort") or "newest"
        if video_sort not in VIDEO_SORTS:
            raise ValueError(f"video_sort must be one of: {', '.join(sorted(VIDEO_SORTS))}.")
        video_format = config.get("format") or "all"
        if video_format not in ("all", "vertical", "landscape"):
            raise ValueError("format must be one of: all, vertical, landscape.")
        min_duration = _nonnegative_int(config.get("min_duration_seconds"), "min_duration_seconds")
        max_duration = _nonnegative_int(config.get("max_duration_seconds"), "max_duration_seconds")
        if min_duration is not None and max_duration is not None and min_duration > max_duration:
            raise ValueError("min_duration_seconds cannot be greater than max_duration_seconds.")
        return {
            "video_type": _optional_int(config.get("video_type"), "video_type"),
            "standalone_only": bool(config.get("standalone_only")),
            "video_sort": video_sort,
            "format": video_format,
            "min_duration_seconds": min_duration,
            "max_duration_seconds": max_duration,
            "language": _text(config.get("language"), "language", 10),
            "story_genres": _int_list(config.get("story_genres"), "story_genres"),
            "video_ids": _int_list(config.get("video_ids"), "video_ids", max_len=24),
        }
    if section_type == HomeSection.TYPE_BLOGS:
        blog_sort = config.get("blog_sort") or "newest"
        if blog_sort not in BLOG_SORTS:
            raise ValueError(f"blog_sort must be one of: {', '.join(sorted(BLOG_SORTS))}.")
        return {
            "blog_sort": blog_sort,
            "topics": _int_list(config.get("topics"), "topics"),
            "published_within_days": _bounded_int(config.get("published_within_days"), "published_within_days", 1, 3650),
            "story_genres": _int_list(config.get("story_genres"), "story_genres"),
            "only_with_story": bool(config.get("only_with_story")),
            "pin_blog_ids": _int_list(config.get("pin_blog_ids"), "pin_blog_ids", max_len=6),
            "blog_ids": _int_list(config.get("blog_ids"), "blog_ids", max_len=24),
        }
    if section_type == HomeSection.TYPE_BANNER:
        theme = config.get("theme") or "ocean"
        if theme not in BANNER_THEMES:
            raise ValueError(f"theme must be one of: {', '.join(BANNER_THEMES)}.")
        return {
            "button_label": _text(config.get("button_label"), "button_label", 40),
            "button_url": _text(config.get("button_url"), "button_url", 300),
            "theme": theme,
        }
    if section_type == HomeSection.TYPE_AD_SLOT:
        size = config.get("ad_size") or "banner"
        if size not in AD_SIZES:
            raise ValueError(f"ad_size must be one of: {', '.join(AD_SIZES)}.")
        return {"ad_size": size}
    return {}


def _fetch_count(section):
    if section.allow_repeats:
        return section.item_limit
    return min(section.item_limit + DEDUPE_BUFFER, MAX_ITEMS + DEDUPE_BUFFER)


def _reading_time_expression():
    # This matches story_reading_minutes_cached(): chapter time wins when it
    # exists, otherwise the cached file-derived estimate is used.
    return Coalesce("cached_chapter_reading_minutes", "cached_file_reading_minutes")


def _published_views_median():
    value = cache.get(HIDDEN_GEMS_MEDIAN_CACHE_KEY)
    if value is None:
        views = list(Story.objects.published().order_by("views").values_list("views", flat=True))
        value = median(views) if views else 0
        cache.set(HIDDEN_GEMS_MEDIAN_CACHE_KEY, value, HIDDEN_GEMS_MEDIAN_CACHE_SECONDS)
    return value


def _apply_story_exclusions(qs, config):
    if config.get("exclude_tags"):
        qs = qs.exclude(
            pk__in=Story.objects.filter(tags__in=config["exclude_tags"]).values("pk")
        )
    if config.get("exclude_story_ids"):
        qs = qs.exclude(pk__in=config["exclude_story_ids"])
    return qs


def _ordered_story_list(qs, ids):
    position = {story_id: index for index, story_id in enumerate(ids)}
    return sorted(qs.filter(pk__in=ids), key=lambda story: position[story.pk])


def _ordered_object_list(qs, ids):
    position = {object_id: index for index, object_id in enumerate(ids)}
    return sorted(qs.filter(pk__in=ids), key=lambda obj: position[obj.pk])


def story_queryset(section, *, with_total=False):
    """Published stories matching a `stories_query` / `stories_manual` section,
    in its order, before slicing."""
    config = section.config or {}
    qs = Story.objects.published().for_card_list().annotate(
        _reading_minutes=_reading_time_expression()
    )

    if section.section_type == HomeSection.TYPE_STORIES_MANUAL:
        ids = config.get("story_ids") or []
        position = {story_id: i for i, story_id in enumerate(ids)}
        stories = sorted(qs.filter(pk__in=ids), key=lambda s: position.get(s.pk, 0))
        return (stories, len(stories)) if with_total else stories

    # M2M filters go through pk__in subqueries rather than joins, so a story
    # matching several selected genres can't appear twice.
    for field, key in (("genres", "genres"), ("categories", "categories"), ("tags", "tags"), ("themes", "themes")):
        ids = config.get(key)
        if ids:
            qs = qs.filter(pk__in=Story.objects.filter(**{f"{field}__in": ids}).values("pk"))
    moods = config.get("moods")
    if moods:
        qs = qs.filter(
            pk__in=Story.objects.filter(public_story_mood_q(), story_moods__mood__slug__in=moods).values("pk")
        )
    if config.get("story_type"):
        qs = qs.filter(story_type_id=config["story_type"])
    if config.get("language") and config["language"].lower() != "all":
        qs = qs.filter(language=config["language"])
    if config.get("countries"):
        qs = qs.filter(country__in=config["countries"])
    if config.get("authors"):
        qs = qs.filter(author_id__in=config["authors"])
    if config.get("has_summary"):
        qs = qs.exclude(Q(summary__isnull=True) | Q(summary__exact=""))
    if config.get("has_audio"):
        qs = qs.filter(pk__in=Audio.objects.values("story_id"))
    if config.get("has_video"):
        qs = qs.filter(pk__in=Video.objects.exclude(story__isnull=True).values("story_id"))
    if config.get("has_read_along"):
        qs = qs.filter(
            pk__in=Audio.objects.exclude(transcript="").exclude(audio_file="").values("story_id")
        )
    if config.get("require_cover"):
        # A story with neither cover would render the placeholder tile.
        qs = qs.exclude(missing_cover_q())
    if config.get("original_only"):
        qs = with_preferred_translation_only(qs.filter(is_original=True))
    if config.get("min_reading_minutes") is not None:
        qs = qs.filter(_reading_minutes__gte=config["min_reading_minutes"])
    if config.get("max_reading_minutes") is not None:
        qs = qs.filter(_reading_minutes__lte=config["max_reading_minutes"])
    if config.get("published_within_days") is not None:
        qs = qs.filter(
            site_published_date__gte=timezone.localdate()
            - timedelta(days=config["published_within_days"])
        )
    if config.get("min_rating") is not None:
        qs = qs.filter(rating__gte=config["min_rating"])
    if config.get("min_ratings_count") is not None:
        qs = qs.filter(_reviews_count__gte=config["min_ratings_count"])

    qs = _apply_story_exclusions(qs, config)
    pin_ids = config.get("pin_story_ids") or []
    if pin_ids:
        pin_qs = _apply_story_exclusions(
            Story.objects.published().for_card_list().annotate(
                _reading_minutes=_reading_time_expression()
            ),
            config,
        )
        pinned = _ordered_story_list(pin_qs, pin_ids)
        pinned_ids = [story.pk for story in pinned]
        qs = qs.exclude(pk__in=pinned_ids)
    else:
        pinned = []

    if config.get("sort") == TRENDING_SORT:
        from .home_metrics import trending_story_ids

        ids = trending_story_ids(config.get("trending_days", 7))
        rank = Case(*[When(pk=story_id, then=position) for position, story_id in enumerate(ids)], default=len(ids), output_field=IntegerField())
        qs = qs.filter(pk__in=ids).order_by(rank, "-id")
    elif config.get("sort") == "shortest":
        qs = qs.order_by(F("_reading_minutes").asc(nulls_last=True), "-id")
    elif config.get("sort") == "longest":
        qs = qs.order_by(F("_reading_minutes").desc(nulls_last=True), "-id")
    elif config.get("sort") == "most_completed":
        qs = qs.annotate(_completion_count=Count("completions", distinct=True)).order_by(
            "-_completion_count", "-views"
        )
    elif config.get("sort") == "hidden_gems":
        qs = qs.filter(rating__gt=0, views__lte=_published_views_median()).order_by(
            "-rating", "views"
        )
    elif config.get("sort") == "daily_shuffle":
        total = qs.count() + len(pinned) if with_total else None
        candidate_ids = list(qs.order_by("pk").values_list("pk", flat=True)[:500])
        random.Random(f"{timezone.localdate().isoformat()}-{section.pk}").shuffle(candidate_ids)
        qs = _ordered_story_list(qs, candidate_ids[: _fetch_count(section)])
    else:
        qs = qs.order_by(*STORY_SORTS.get(config.get("sort"), STORY_SORTS["newest"]))

    if config.get("sort") != "daily_shuffle":
        total = qs.count() + len(pinned) if with_total else None
    if pinned:
        remaining = max(0, _fetch_count(section) - len(pinned))
        result = pinned + list(qs[:remaining])
    else:
        result = qs
    return (result, total) if with_total else result


def missing_cover_q():
    """Stories with neither an uploaded cover nor a cover URL — the same
    definition the Analytics "catalogue gaps" use for `no_cover`."""
    return (Q(cover_image_file__isnull=True) | Q(cover_image_file="")) & (
        Q(cover_image__isnull=True) | Q(cover_image="")
    )


def video_queryset(section, *, with_total=False):
    config = section.config or {}
    qs = (
        Video.objects.select_related("story", "story__author", "video_type", "series")
        .prefetch_related(
            "story__audios",
            "story__chapters",
            Prefetch(
                "story__videos",
                queryset=Video.objects.select_related("series").order_by("order", "id"),
                to_attr="home_section_videos",
            ),
        )
        .filter(Q(story__isnull=True) | published_story_q("story"))
    )
    picked_ids = config.get("video_ids") or []
    if picked_ids:
        videos = _ordered_object_list(qs, picked_ids)
        return (videos, len(videos)) if with_total else videos
    if config.get("video_type"):
        qs = qs.filter(video_type_id=config["video_type"])
    if config.get("standalone_only"):
        qs = qs.filter(story__isnull=True)
    video_format = config.get("format") or "all"
    if video_format == "vertical":
        qs = qs.filter(aspect_ratio="9:16")
    elif video_format == "landscape":
        qs = qs.filter(aspect_ratio="16:9")
    if config.get("min_duration_seconds") is not None:
        qs = qs.filter(duration_seconds__gte=config["min_duration_seconds"])
    if config.get("max_duration_seconds") is not None:
        qs = qs.filter(duration_seconds__lte=config["max_duration_seconds"])
    if config.get("language") and config["language"].lower() != "all":
        # Standalone videos have no language field, so this setting is skipped
        # for them while story-backed videos must match their story language.
        qs = qs.filter(Q(story__isnull=True) | Q(story__language=config["language"]))
    if config.get("story_genres"):
        qs = qs.filter(
            story_id__in=Story.objects.filter(genres__in=config["story_genres"]).values("pk")
        )

    video_sort = config.get("video_sort") or "newest"
    total = qs.count() if with_total else None
    if video_sort == "oldest":
        result = qs.order_by("created_at", "id")
    elif video_sort == "shortest":
        result = qs.order_by(F("duration_seconds").asc(nulls_last=True), "-id")
    elif video_sort == "longest":
        result = qs.order_by(F("duration_seconds").desc(nulls_last=True), "-id")
    elif video_sort == "daily_shuffle":
        ids = list(qs.order_by("pk").values_list("pk", flat=True)[:500])
        random.Random(f"{timezone.localdate().isoformat()}-{section.pk}").shuffle(ids)
        result = _ordered_object_list(qs, ids[: section.item_limit])
    else:
        result = qs.order_by("-created_at", "-id")
    return (result, total) if with_total else result


def _blog_base_queryset():
    return Blog.objects.published().prefetch_related(
        "topics",
        Prefetch(
            "linked_stories",
            queryset=Story.objects.published(),
            to_attr="published_linked_stories",
        ),
    )


def blog_queryset(section, *, with_total=False):
    config = section.config or {}
    qs = _blog_base_queryset()
    picked_ids = config.get("blog_ids") or []
    if picked_ids:
        blogs = _ordered_object_list(qs, picked_ids)
        return (blogs, len(blogs)) if with_total else blogs

    if config.get("topics"):
        qs = qs.filter(
            pk__in=Blog.objects.filter(topics__in=config["topics"]).values("pk")
        )
    if config.get("published_within_days") is not None:
        qs = qs.filter(
            created_at__gte=timezone.now() - timedelta(days=config["published_within_days"])
        )
    if config.get("story_genres"):
        qs = qs.filter(
            pk__in=Blog.objects.filter(
                linked_stories__in=Story.objects.published().filter(
                    genres__in=config["story_genres"]
                )
            ).values("pk")
        )
    if config.get("only_with_story"):
        qs = qs.filter(
            pk__in=Blog.objects.filter(
                linked_stories__in=Story.objects.published()
            ).values("pk")
        )

    pin_ids = config.get("pin_blog_ids") or []
    if pin_ids:
        pinned = _ordered_object_list(_blog_base_queryset(), pin_ids)
        pinned_ids = [blog.pk for blog in pinned]
        qs = qs.exclude(pk__in=pinned_ids)
    else:
        pinned = []

    total = qs.count() + len(pinned) if with_total else None

    blog_sort = config.get("blog_sort") or "newest"
    if blog_sort == "oldest":
        qs = qs.order_by("created_at", "id")
    elif blog_sort == "shortest":
        qs = qs.order_by(F("reading_time_minutes").asc(nulls_last=True), "-id")
    elif blog_sort == "daily_shuffle":
        ids = list(qs.order_by("pk").values_list("pk", flat=True)[:500])
        random.Random(f"{timezone.localdate().isoformat()}-{section.pk}").shuffle(ids)
        qs = _ordered_object_list(qs, ids[: section.item_limit])
    else:
        qs = qs.order_by("-created_at", "-id")

    if pinned:
        remaining = max(0, section.item_limit - len(pinned))
        result = pinned + list(qs[:remaining])
    else:
        result = qs
    return (result, total) if with_total else result


def default_see_all_url(section):
    """The link used when an editor leaves "See all" blank: a type-appropriate
    page, narrowed to the section's own filter where Library can express it."""
    if section.section_type == HomeSection.TYPE_VIDEOS:
        return "/watch"
    if section.section_type == HomeSection.TYPE_BLOGS:
        return "/blog"
    if section.section_type == HomeSection.TYPE_RECOMMENDED:
        return "/library"
    if section.variant == HomeSection.VARIANT_QUICK_READ:
        return "/quick-reads"
    if section.variant == HomeSection.VARIANT_ORIGINALS:
        return "/originals"
    config = section.config or {}
    params = {}
    # Library takes one genre / category; a multi-value section links to the first.
    if config.get("genres"):
        params["genre"] = config["genres"][0]
    if config.get("categories"):
        params["category"] = config["categories"][0]
    if config.get("moods"):
        params["moods"] = ",".join(config["moods"])
    if config.get("story_type"):
        params["story_type"] = config["story_type"]
    if config.get("language"):
        params["language"] = config["language"]
    return "/library" + (f"?{urlencode(params)}" if params else "")


def _blog_cards(blogs, request):
    from .serializers import BlogCardSerializer

    return BlogCardSerializer(blogs, many=True, context={"request": request}).data


def section_is_visible(section, *, signed_in, now):
    """Audience + schedule gate for one section. `ends_at` is exclusive."""
    if section.show_to == HomeSection.SHOW_TO_SIGNED_IN and not signed_in:
        return False
    if section.show_to == HomeSection.SHOW_TO_SIGNED_OUT and signed_in:
        return False
    if section.starts_at and section.starts_at > now:
        return False
    if section.ends_at and section.ends_at <= now:
        return False
    return True


def resolve_home_sections(request):
    # Imported here: serializers import models, and this module is imported by api.
    from .serializers import PublicVideoSerializer, StoryListSerializer

    context = {"request": request}
    resolved = []
    now = timezone.now()
    user = getattr(request, "user", None)
    signed_in = bool(user and user.is_authenticated)
    for section in HomeSection.objects.filter(is_enabled=True).order_by("position", "id"):
        if not section_is_visible(section, signed_in=signed_in, now=now):
            continue
        kind = section.section_type
        items = []
        if kind == HomeSection.TYPE_STORIES_QUERY or kind == HomeSection.TYPE_STORIES_MANUAL:
            stories = story_queryset(section)
            stories = stories[: _fetch_count(section)]
            items = StoryListSerializer(stories, many=True, context=context).data
        elif kind == HomeSection.TYPE_VIDEOS:
            items = PublicVideoSerializer(
                video_queryset(section)[: section.item_limit], many=True, context=context
            ).data
        elif kind == HomeSection.TYPE_BLOGS:
            blogs = blog_queryset(section)[: section.item_limit]
            items = _blog_cards(blogs, request)
        # A content section with nothing to show is left out entirely rather
        # than rendered as an empty heading.
        if kind in HomeSection.CONTENT_TYPES and not items:
            continue

        see_all_url = ""
        if section.show_see_all and kind in HomeSection.CONTENT_TYPES | {HomeSection.TYPE_RECOMMENDED}:
            see_all_url = section.see_all_url or default_see_all_url(section)
        resolved.append(
            {
                "id": section.pk,
                "section_type": kind,
                "variant": section.variant,
                "title": section.title,
                "subtitle": section.subtitle,
                "icon": section.icon if section.icon in HOME_SECTION_ICON_NAMES else "",
                "layout": section.layout,
                "columns_mobile": section.columns_mobile,
                "columns_desktop": section.columns_desktop,
                "card_style": section.card_style,
                "item_limit": section.item_limit,
                "allow_repeats": section.allow_repeats,
                "see_all_url": see_all_url,
                "see_all_label": section.see_all_label or "See all",
                "config": section.config if kind in (HomeSection.TYPE_BANNER, HomeSection.TYPE_AD_SLOT) else {},
                "items": items,
            }
        )
    return resolved

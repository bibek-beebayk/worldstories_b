"""Library page support: facet counts, collection tiles and bulk progress.

All three read the same catalogue the list endpoint shows — published stories,
one edition per translation group — and apply the list endpoint's own filters
(StoryFilter), so a count here is exactly what the matching list request would
return.
"""

import hashlib
import json
from collections import Counter

from django.core.cache import cache
from django.db.models import Count, Q
from django.http import QueryDict

from apps.stats.models import (
    AudioReadingProgress,
    ChapterReadingProgress,
    StoryCompletion,
    VideoWatchProgress,
)

from .filters import FORMATS, LENGTH_BUCKETS, StoryFilter, apply_format, with_reading_minutes
from .home_sections import missing_cover_q
from .models import (
    COUNTRY_CHOICES,
    Author,
    Category,
    Chapter,
    Genre,
    Mood,
    Story,
    StoryMood,
    StoryType,
    with_preferred_translation_only,
)
from .serializers import CARD_COVER_SIZE

from core.libs.images import get_cover_image_url

FACETS_CACHE_SECONDS = 60 * 5
COLLECTIONS_CACHE_SECONDS = 60 * 60
COUNTRY_NAMES = dict(COUNTRY_CHOICES)

LENGTH_LABELS = {
    "under_10": "Under 10 min",
    "10_60": "10–60 min",
    "1_5h": "1–5 hours",
    "5h_plus": "5+ hours",
}

# Facet -> the request params that narrow by it. When counting a facet, its own
# params are dropped so the other options in the same facet stay visible with
# their counts ("add Romance" shows how many Romance stories there would be).
FACET_PARAMS = {
    "genres": {"genres", "genre"},
    "categories": {"categories", "category"},
    "languages": {"language"},
    "lengths": {"length"},
    "countries": {"country"},
    "types": {"story_type"},
    "statuses": {"status"},
    "formats": {"format", "has_audio", "has_video", "has_summary", "has_read_along"},
    "originals": {"is_original", "original"},
    "moods": {"moods"},
}
# Not filters: pagination, ordering, and the Nepali-site switch (handled apart).
NON_FILTER_PARAMS = {"page", "sort", "page_size", "show_in_nepali_site"}


def catalogue(*, nepali_only=False, preferred_language=None):
    """The listed catalogue: published, one edition per translation group."""
    return with_preferred_translation_only(
        Story.objects.published(),
        preferred_language=preferred_language,
        candidate_filter=Q(show_in_nepali_site=True) if nepali_only else None,
    )


def filtered(params, *, nepali_only=False, drop=()):
    """The catalogue narrowed by `params` (a dict of the list endpoint's filter
    params), leaving out any param named in `drop`."""
    data = QueryDict(mutable=True)
    for key, value in params.items():
        if key in NON_FILTER_PARAMS or key in drop or value in (None, ""):
            continue
        data[key] = value
    if nepali_only:
        data["show_in_nepali_site"] = "true"
    # Translation collapsing prefers the requested language; when counting the
    # language facet itself that preference is off, so every language is offered.
    preferred = None if "language" in drop else params.get("language")
    queryset = catalogue(nepali_only=nepali_only, preferred_language=preferred)
    return StoryFilter(data, queryset=queryset).qs


def _ids(queryset):
    return queryset.order_by().values("pk")


def _counts(rows):
    return {key: total for key, total in rows}


# --- facets ------------------------------------------------------------------


def _taxonomy_facet(model, through_field, base_ids):
    through = getattr(Story, through_field).through
    fk = model.__name__.lower()
    counts = _counts(
        through.objects.filter(story_id__in=base_ids)
        .values_list(f"{fk}_id")
        .annotate(total=Count("story_id", distinct=True))
    )
    rows = [
        {"id": pk, "slug": slug or str(pk), "name": name, "count": counts[pk]}
        for pk, slug, name in model.objects.filter(pk__in=counts).values_list("id", "slug", "name")
    ]
    return sorted(rows, key=lambda row: (-row["count"], row["name"].lower()))


def compute_facets(params, *, nepali_only=False):
    def base(facet):
        return _ids(filtered(params, nepali_only=nepali_only, drop=FACET_PARAMS[facet]))

    stories = Story.objects

    languages = Counter(
        dict(stories.filter(pk__in=base("languages")).values_list("language").annotate(n=Count("pk")))
    )
    countries = dict(
        stories.filter(pk__in=base("countries")).exclude(country="").values_list("country").annotate(n=Count("pk"))
    )
    types = dict(stories.filter(pk__in=base("types")).values_list("story_type_id").annotate(n=Count("pk")))
    type_names = dict(StoryType.objects.filter(pk__in=types).values_list("id", "name"))

    length_base = with_reading_minutes(stories.filter(pk__in=base("lengths")))
    lengths = []
    for key, (low, high) in LENGTH_BUCKETS.items():
        bucket = length_base
        if low is not None:
            bucket = bucket.filter(_minutes__gte=low)
        if high is not None:
            bucket = bucket.filter(_minutes__lt=high)
        lengths.append({"value": key, "label": LENGTH_LABELS[key], "count": bucket.count()})

    status_base = stories.filter(pk__in=base("statuses"))
    statuses = [
        {"value": "completed", "label": "Completed", "count": status_base.filter(is_completed=True).count()},
        {"value": "ongoing", "label": "Ongoing", "count": status_base.filter(is_completed=False).count()},
    ]

    format_base = filtered(params, nepali_only=nepali_only, drop=FACET_PARAMS["formats"])
    formats = [{"value": "all", "count": format_base.count()}] + [
        {"value": fmt, "count": apply_format(format_base, fmt).count()} for fmt in FORMATS
    ]

    mood_counts = _counts(
        StoryMood.objects.filter(story_id__in=base("moods"))
        # Same rule as public_story_mood_q(): an admin's pick, or a confirmed suggestion.
        .filter(Q(source=StoryMood.SOURCE_ADMIN) | Q(reviewed=True))
        .values_list("mood_id")
        .annotate(n=Count("story_id", distinct=True))
    )
    moods = [
        {"slug": slug, "name": name, "count": mood_counts[pk]}
        for pk, slug, name in Mood.objects.filter(pk__in=mood_counts).values_list("id", "slug", "name")
    ]

    return {
        "total": filtered(params, nepali_only=nepali_only).count(),
        "genres": _taxonomy_facet(Genre, "genres", base("genres")),
        "categories": _taxonomy_facet(Category, "categories", base("categories")),
        "languages": [
            {"value": code, "count": n} for code, n in sorted(languages.items(), key=lambda kv: (-kv[1], kv[0])) if code
        ],
        "lengths": lengths,
        "countries": [
            {"value": code, "label": COUNTRY_NAMES.get(code, code), "count": n}
            for code, n in sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "types": [
            {"id": pk, "name": type_names.get(pk, str(pk)), "count": n}
            for pk, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
            if pk
        ],
        "statuses": statuses,
        "formats": formats,
        "originals": {
            "count": stories.filter(pk__in=base("originals"), is_original=True).count(),
        },
        "moods": sorted(moods, key=lambda row: (-row["count"], row["name"])),
    }


def _cache_key(prefix, params, nepali_only):
    normalized = json.dumps(
        sorted((k, v) for k, v in params.items() if k not in NON_FILTER_PARAMS and v not in (None, "")),
        ensure_ascii=False,
    )
    digest = hashlib.sha1(f"{normalized}|{nepali_only}".encode()).hexdigest()
    return f"library:{prefix}:{digest}"


def get_facets(params, *, nepali_only=False):
    key = _cache_key("facets", params, nepali_only)
    data = cache.get(key)
    if data is None:
        data = compute_facets(params, nepali_only=nepali_only)
        cache.set(key, data, FACETS_CACHE_SECONDS)
    return data


# --- collections -----------------------------------------------------------------

GENRE_TILES = 4
COUNTRY_TILES = 2
MIN_TILE_STORIES = 3
FEATURED_AUTHOR_NAME = "conan doyle"


def _covers(queryset, request):
    stories = (
        Story.objects.filter(pk__in=_ids(queryset))
        .exclude(missing_cover_q())
        .order_by("-views", "-id")
        .only("id", "cover_image", "cover_image_file")[:3]
    )
    return [get_cover_image_url(s.cover_image_file, s.cover_image, request, size=CARD_COVER_SIZE) for s in stories]


def compute_collections(request, *, nepali_only=False):
    tiles = []

    def add(key, label, params):
        queryset = filtered(params, nepali_only=nepali_only)
        count = queryset.count()
        if count >= MIN_TILE_STORIES:
            tiles.append({"key": key, "label": label, "params": params, "count": count, "covers": _covers(queryset, request)})

    add("originals", "WorldStories Originals", {"original": "true"})
    add("nepali", "Nepali stories", {"language": "ne"})
    add("under_10", "Under 10 minutes", {"length": "under_10"})

    base = catalogue(nepali_only=nepali_only)
    genre_counts = dict(
        Story.genres.through.objects.filter(story_id__in=_ids(base))
        .values_list("genre_id")
        .annotate(n=Count("story_id", distinct=True))
    )
    top_genres = sorted(genre_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:GENRE_TILES]
    genres = {g.pk: g for g in Genre.objects.filter(pk__in=[pk for pk, _n in top_genres])}
    for pk, _n in top_genres:
        genre = genres[pk]
        add(f"genre-{pk}", genre.name, {"genre": genre.slug or str(pk)})

    country_counts = dict(
        Story.objects.filter(pk__in=_ids(base)).exclude(country="").values_list("country").annotate(n=Count("pk"))
    )
    for code, _n in sorted(country_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:COUNTRY_TILES]:
        add(f"country-{code}", f"Stories from {COUNTRY_NAMES.get(code, code)}", {"country": code})

    # One featured author: Arthur Conan Doyle if the catalogue has him, otherwise
    # whoever has the most listed stories.
    author_counts = dict(
        Story.objects.filter(pk__in=_ids(base), author__isnull=False).values_list("author_id").annotate(n=Count("pk"))
    )
    featured = None
    for author in Author.objects.filter(pk__in=author_counts, name__icontains=FEATURED_AUTHOR_NAME)[:1]:
        featured = author
    if featured is None and author_counts:
        top_id = max(author_counts, key=lambda pk: (author_counts[pk], -pk))
        featured = Author.objects.filter(pk=top_id).first()
    if featured is not None:
        add(f"author-{featured.pk}", featured.name, {"author": str(featured.pk)})
    return tiles


def get_collections(request, *, nepali_only=False):
    # Cover URLs are absolute, so the cache is per host.
    key = f"library:collections:{request.get_host()}:{nepali_only}"
    data = cache.get(key)
    if data is None:
        data = compute_collections(request, nepali_only=nepali_only)
        cache.set(key, data, COLLECTIONS_CACHE_SECONDS)
    return data


# --- bulk progress ----------------------------------------------------------------

MAX_PROGRESS_IDS = 60


def story_progress(user, story_ids):
    """{story_id: 0..1} for stories this reader has started: the furthest of
    chapter reading (chapters done / chapters), audio and video progress; 1.0 for
    a finished story. Untouched stories are omitted."""
    story_ids = list(dict.fromkeys(story_ids))[:MAX_PROGRESS_IDS]
    progress = {}

    chapter_totals = dict(
        Chapter.objects.filter(story_id__in=story_ids).values_list("story_id").annotate(n=Count("pk"))
    )
    chapter_sums = {}
    for story_id, value in ChapterReadingProgress.objects.filter(user=user, story_id__in=story_ids).values_list(
        "story_id", "progress"
    ):
        chapter_sums[story_id] = chapter_sums.get(story_id, 0.0) + max(0.0, min(1.0, value))
    for story_id, total in chapter_sums.items():
        if chapter_totals.get(story_id):
            progress[story_id] = max(progress.get(story_id, 0.0), total / chapter_totals[story_id])

    for model in (AudioReadingProgress, VideoWatchProgress):
        for story_id, value in model.objects.filter(user=user, story_id__in=story_ids).values_list("story_id", "progress"):
            progress[story_id] = max(progress.get(story_id, 0.0), max(0.0, min(1.0, value)))

    for story_id in StoryCompletion.objects.filter(user=user, story_id__in=story_ids).values_list("story_id", flat=True):
        progress[story_id] = 1.0

    return {story_id: round(min(value, 1.0), 4) for story_id, value in progress.items() if value > 0}

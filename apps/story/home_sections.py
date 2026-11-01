"""Resolution and validation of the admin-managed homepage sections.

`resolve_home_sections` turns the enabled `HomeSection` rows into the public
`sections` array served by /api/home/: content sections carry their already
resolved items (so the SSR loader can render them on first paint), widget
sections carry only their presentation settings.
"""

from urllib.parse import urlencode

from django.db.models import Q

from .filters import public_story_mood_q
from .models import (
    HOME_SECTION_ICON_NAMES,
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
}
BANNER_THEMES = ["ocean", "violet", "rose", "amber", "emerald"]
AD_SIZES = ["banner", "square", "rectangle"]

# What the admin form offers per type. Served by the `types` endpoint so the
# form needs no per-type knowledge of its own beyond rendering these fields.
TYPE_META = {
    HomeSection.TYPE_STORIES_QUERY: {"content": True, "fields": ["layout", "item_limit", "card_style", "allow_repeats", "see_all", "icon", "variant", "filters"]},
    HomeSection.TYPE_STORIES_MANUAL: {"content": True, "fields": ["layout", "item_limit", "card_style", "allow_repeats", "see_all", "icon", "variant", "story_ids"]},
    HomeSection.TYPE_VIDEOS: {"content": True, "fields": ["layout", "item_limit", "see_all", "icon", "video_filters"]},
    HomeSection.TYPE_BLOGS: {"content": True, "fields": ["layout", "item_limit", "see_all", "icon"]},
    HomeSection.TYPE_BANNER: {"content": False, "fields": ["icon", "banner"]},
    HomeSection.TYPE_AD_SLOT: {"content": False, "fields": ["ad_size"]},
    HomeSection.TYPE_NEW_ENTRIES: {"content": False, "fields": ["icon"]},
    HomeSection.TYPE_CONTINUE_SHELF: {"content": False, "fields": []},
    HomeSection.TYPE_READING_JOURNEY: {"content": False, "fields": []},
    HomeSection.TYPE_RECOMMENDED: {"content": False, "fields": ["see_all", "icon"]},
    HomeSection.TYPE_JOURNEYS_BANNER: {"content": False, "fields": []},
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
        }
    if section_type == HomeSection.TYPE_STORIES_MANUAL:
        return {"story_ids": _int_list(config.get("story_ids"), "story_ids", max_len=48)}
    if section_type == HomeSection.TYPE_VIDEOS:
        return {
            "video_type": _optional_int(config.get("video_type"), "video_type"),
            "standalone_only": bool(config.get("standalone_only")),
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


def story_queryset(section):
    """Published stories matching a `stories_query` / `stories_manual` section,
    in its order, before slicing."""
    config = section.config or {}
    qs = Story.objects.published().for_card_list()

    if section.section_type == HomeSection.TYPE_STORIES_MANUAL:
        ids = config.get("story_ids") or []
        position = {story_id: i for i, story_id in enumerate(ids)}
        return sorted(qs.filter(pk__in=ids), key=lambda s: position.get(s.pk, 0))

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
    if config.get("original_only"):
        qs = with_preferred_translation_only(qs.filter(is_original=True))
    return qs.order_by(*STORY_SORTS.get(config.get("sort"), STORY_SORTS["newest"]))


def video_queryset(section):
    config = section.config or {}
    qs = Video.objects.select_related("story", "video_type").filter(
        Q(story__isnull=True) | published_story_q("story")
    )
    if config.get("video_type"):
        qs = qs.filter(video_type_id=config["video_type"])
    if config.get("standalone_only"):
        qs = qs.filter(story__isnull=True)
    return qs.order_by("-created_at", "-id")


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


def resolve_home_sections(request):
    # Imported here: serializers import models, and this module is imported by api.
    from .serializers import PublicVideoSerializer, StoryListSerializer

    context = {"request": request}
    resolved = []
    for section in HomeSection.objects.filter(is_enabled=True).order_by("position", "id"):
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
            items = _blog_cards(Blog.objects.published().order_by("-created_at")[: section.item_limit], request)
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

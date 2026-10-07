"""Page block types: what each one may contain, and what the public page gets.

Every block's config passes through clean_block() before it is stored, so the
database only ever holds known keys with checked values — rich text is
sanitized here, links are restricted to site paths and http(s) URLs, colours
to hex. resolve_block() turns a stored block into the public payload, filling
story blocks with card data so the page renders in one server-side request.
"""
import re

from django.core.exceptions import ValidationError

from apps.story.models import (
    COUNTRY_CHOICES,
    Category,
    Genre,
    Story,
    Tag,
    Theme,
    with_preferred_translation_only,
)
from apps.story.rich_text import sanitize_reader_html
from apps.story.serializers import StoryListSerializer

from .models import Page, PageBlock

MAX_LISTED_STORIES = 48
MAX_QUERY_LIMIT = 24
MAX_FAQ_ITEMS = 30
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
COUNTRY_CODES = {code for code, _label in COUNTRY_CHOICES}

QUERY_SORTS = {
    "newest": ("-site_published_date", "-id"),
    "popular": ("-views", "-id"),
    "top_rated": ("-rating", "-views", "-id"),
}
# filter key -> (model, Story lookup, public "see all" path prefix)
QUERY_TAXONOMIES = {
    "genre": (Genre, "genres__slug", "/genre/"),
    "category": (Category, "categories__slug", "/category/"),
    "tag": (Tag, "tags__slug", "/tag/"),
    "theme": (Theme, "themes__slug", "/theme/"),
}

# The blocks a new page starts with, per template. Only a starting point —
# the editor can add, remove and reorder from there.
TEMPLATE_BLOCKS = {
    Page.TEMPLATE_STATIC: [
        (PageBlock.TYPE_RICH_TEXT, {"html": "<p>Write your page here.</p>"}),
    ],
    Page.TEMPLATE_BOOK_LIST: [
        (PageBlock.TYPE_RICH_TEXT, {"html": "<p>Introduce this list here.</p>"}),
        (PageBlock.TYPE_STORY_LIST, {"heading": "", "story_ids": [], "layout": "grid"}),
    ],
    Page.TEMPLATE_BLANK: [],
}


def _text(config, key, max_length, *, required=False, label=None):
    value = config.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{label or key} must be text.")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{label or key} is required.")
    if len(value) > max_length:
        raise ValidationError(f"{label or key} must be at most {max_length} characters.")
    return value


def _link(config, key, *, required=False, label=None):
    """A site path ("/library") or a full http(s) URL — never javascript:,
    data: or a protocol-relative //host link."""
    value = _text(config, key, 500, required=required, label=label)
    if not value:
        return value
    is_site_path = value.startswith("/") and not value.startswith("//")
    if not (is_site_path or value.startswith(("https://", "http://"))):
        raise ValidationError(f"{label or key} must be a site path like /library or a full https:// URL.")
    return value


def _image(config, key, *, required=False, label=None):
    value = _text(config, key, 500, required=required, label=label)
    if value and not value.startswith(("https://", "http://")):
        raise ValidationError(f"{label or key} must be a full https:// image URL.")
    return value


def _color(config, key, default):
    value = config.get(key) or default
    if not isinstance(value, str) or not HEX_COLOR.match(value):
        raise ValidationError(f"{key} must be a 6-digit hex colour such as #ed405a.")
    return value


def _choice(config, key, choices, default):
    value = config.get(key) or default
    if value not in choices:
        raise ValidationError(f"{key} must be one of: {', '.join(choices)}.")
    return value


def _clean_rich_text(config):
    html = config.get("html", "")
    if not isinstance(html, str):
        raise ValidationError("html must be text.")
    return {"html": sanitize_reader_html(html)}


def _clean_story_list(config):
    raw_ids = config.get("story_ids") or []
    if not isinstance(raw_ids, list):
        raise ValidationError("story_ids must be a list.")
    story_ids = []
    for raw in raw_ids:
        try:
            story_id = int(raw)
        except (TypeError, ValueError):
            raise ValidationError("story_ids must be story ids.")
        if story_id not in story_ids:
            story_ids.append(story_id)
    if len(story_ids) > MAX_LISTED_STORIES:
        raise ValidationError(f"Pick at most {MAX_LISTED_STORIES} stories.")
    known = set(Story.objects.filter(id__in=story_ids).values_list("id", flat=True))
    return {
        "heading": _text(config, "heading", 120),
        "story_ids": [story_id for story_id in story_ids if story_id in known],
        "layout": _choice(config, "layout", ("grid", "rail"), "grid"),
    }


def _clean_story_query(config):
    cleaned = {
        "heading": _text(config, "heading", 120),
        "sort": _choice(config, "sort", tuple(QUERY_SORTS), "newest"),
        "layout": _choice(config, "layout", ("grid", "rail"), "grid"),
    }
    try:
        limit = int(config.get("limit") or 12)
    except (TypeError, ValueError):
        raise ValidationError("limit must be a number.")
    if not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_QUERY_LIMIT}.")
    cleaned["limit"] = limit

    for key, (model, _lookup, _path) in QUERY_TAXONOMIES.items():
        slug = _text(config, key, 60)
        if slug and not model.objects.filter(slug=slug).exists():
            raise ValidationError(f'No {key} with the slug "{slug}".')
        cleaned[key] = slug

    country = _text(config, "country", 2).upper()
    if country and country not in COUNTRY_CODES:
        raise ValidationError(f'Unknown country code "{country}".')
    cleaned["country"] = country
    return cleaned


def _clean_banner(config):
    return {
        "heading": _text(config, "heading", 120, required=True),
        "text": _text(config, "text", 400),
        "image": _image(config, "image"),
        "cta_label": _text(config, "cta_label", 40),
        "cta_url": _link(config, "cta_url"),
        "background_color": _color(config, "background_color", "#1a212d"),
    }


def _clean_image(config):
    return {
        "url": _image(config, "url", required=True, label="Image URL"),
        # Required: it's what search engines and screen readers get instead.
        "alt": _text(config, "alt", 200, required=True, label="Alt text"),
        "caption": _text(config, "caption", 200),
    }


def _clean_faq(config):
    items = config.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValidationError("Add at least one question.")
    if len(items) > MAX_FAQ_ITEMS:
        raise ValidationError(f"Use at most {MAX_FAQ_ITEMS} questions.")
    cleaned_items = []
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("Each FAQ item needs a question and an answer.")
        cleaned_items.append(
            {
                "question": _text(item, "question", 200, required=True, label="Question"),
                "answer": _text(item, "answer", 2000, required=True, label="Answer"),
            }
        )
    return {"heading": _text(config, "heading", 120), "items": cleaned_items}


def _clean_cta(config):
    return {
        "text": _text(config, "text", 200),
        "label": _text(config, "label", 40, required=True, label="Button label"),
        "url": _link(config, "url", required=True, label="Button link"),
        "bg_color": _color(config, "bg_color", "#ed405a"),
        "text_color": _color(config, "text_color", "#ffffff"),
    }


CLEANERS = {
    PageBlock.TYPE_RICH_TEXT: _clean_rich_text,
    PageBlock.TYPE_STORY_LIST: _clean_story_list,
    PageBlock.TYPE_STORY_QUERY: _clean_story_query,
    PageBlock.TYPE_BANNER: _clean_banner,
    PageBlock.TYPE_IMAGE: _clean_image,
    PageBlock.TYPE_FAQ: _clean_faq,
    PageBlock.TYPE_CTA: _clean_cta,
}


def clean_block(block_type, config):
    """The stored form of a block's config. Raises ValidationError with a
    message an editor can act on."""
    cleaner = CLEANERS.get(block_type)
    if cleaner is None:
        raise ValidationError(f'Unknown block type "{block_type}".')
    if not isinstance(config, dict):
        raise ValidationError("config must be an object.")
    return cleaner(config)


def _story_cards(stories, request):
    return StoryListSerializer(stories, many=True, context={"request": request}).data


def resolve_block(block, request):
    """The public payload for one block."""
    payload = {"id": block.id, "type": block.type, "config": block.config}
    base_qs = Story.objects.published().for_card_list()

    if block.type == PageBlock.TYPE_STORY_LIST:
        ids = block.config.get("story_ids", [])
        by_id = {story.id: story for story in base_qs.filter(id__in=ids)}
        # The editor's order, minus anything no longer published.
        payload["stories"] = _story_cards([by_id[i] for i in ids if i in by_id], request)

    elif block.type == PageBlock.TYPE_STORY_QUERY:
        config = block.config
        qs = base_qs
        see_all = None
        for key, (_model, lookup, path) in QUERY_TAXONOMIES.items():
            if config.get(key):
                qs = qs.filter(**{lookup: config[key]})
                see_all = see_all or f"{path}{config[key]}"
        if config.get("country"):
            qs = qs.filter(country=config["country"])
        qs = with_preferred_translation_only(qs.distinct())
        stories = list(qs.order_by(*QUERY_SORTS[config.get("sort", "newest")])[: config.get("limit", 12)])
        payload["stories"] = _story_cards(stories, request)
        payload["see_all_url"] = see_all

    return payload

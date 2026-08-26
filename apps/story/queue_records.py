"""Shared logic for turning a candidate book record (however it was
sourced — AI suggestion in book_fetch_jobs.py, or a parsed spreadsheet row
in queue_import.py) into either a rejected duplicate or a real StoryQueue
row. Both callers must apply identical dedup/resolution/sanitization rules,
so this lives in one place rather than being reimplemented per source.
"""
from datetime import date
from typing import List, Optional, Tuple

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils.text import slugify

from .book_fetch import _BookRecord
from .models import COUNTRY_CHOICES, Category, Genre, LANGUAGE_CHOICES, Story, StoryQueue, StoryType, Tag, Theme

_COUNTRY_NAME_TO_CODE = {name.lower(): code for code, name in COUNTRY_CHOICES}
_LANGUAGE_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGE_CHOICES}
_COUNTRY_CODES = {code for code, _ in COUNTRY_CHOICES}
_LANGUAGE_CODES = {code for code, _ in LANGUAGE_CHOICES}
_url_validator = URLValidator()

DuplicateReason = str  # "already_a_story" | "already_in_queue" | "duplicate_in_file" | "missing_title"


def existing_title_author_pairs() -> List[Tuple[str, str]]:
    """Raw (title, author) pairs, original casing — every Story row (any
    status — a draft is still "already in the database") plus StoryQueue
    rows not yet turned into a Story (is_added=False — an already-added
    queue row is redundant with its Story counterpart). Same scope as
    StoryQueueViewSet.check_title's query. Used to build the AI-fetch
    prompt's CSV, which needs real casing."""
    pairs = [
        (title, author_name or "")
        for title, author_name in Story.objects.select_related("author").values_list("title", "author__name")
    ]
    pairs += list(StoryQueue.objects.filter(is_added=False).values_list("title", "author_name"))
    return pairs


def normalize_title_author(title: str, author: str) -> Tuple[str, str]:
    return title.strip().lower(), (author or "").strip().lower()


def existing_story_normalized_pairs() -> set:
    return {
        normalize_title_author(title, author_name or "")
        for title, author_name in Story.objects.select_related("author").values_list("title", "author__name")
    }


def existing_queue_normalized_pairs() -> set:
    return {
        normalize_title_author(title, author_name)
        for title, author_name in StoryQueue.objects.filter(is_added=False).values_list("title", "author_name")
    }


def resolve_story_type(raw: str, create_missing: bool = False) -> Optional[StoryType]:
    """Unlike resolve_genres/resolve_categories, create_missing defaults to
    False — story types are meant to be a small, admin-curated set (that's
    the whole point of StoryType being a real, admin-manageable model), so
    an AI suggestion or an import spreadsheet cell should never silently
    spawn a new one. Idempotent when called on the same name more than once
    (e.g. once during import preview, again at confirm) — unlike country/
    language, story types have no separate code/label duality to trip over."""
    name = raw.strip()
    if not name:
        return None
    story_type = StoryType.objects.filter(name__iexact=name).first()
    if story_type is None and create_missing:
        story_type = StoryType.objects.create(name=name)
    return story_type


def resolve_country(raw: str) -> str:
    """raw is a full country NAME (e.g. from Claude or an import spreadsheet
    cell) — resolves it to a COUNTRY_CHOICES code. NOT idempotent: calling
    this again on its own output (already a code, not a name) won't match
    anything and returns "". See validate_country_code for that case."""
    return _COUNTRY_NAME_TO_CODE.get(raw.strip().lower(), "")


def resolve_language(raw: str) -> str:
    """Same as resolve_country but for LANGUAGE_CHOICES — see its docstring."""
    return _LANGUAGE_NAME_TO_CODE.get(raw.strip().lower(), "")


def validate_country_code(code: str) -> str:
    """Confirms an already-resolved country CODE (e.g. a previewed import
    record's country field, which build_preview already ran through
    resolve_country once) is still a real COUNTRY_CHOICES value — passes it
    through unchanged if so, else "". Deliberately does NOT do name->code
    lookup like resolve_country; running that a second time on a code
    instead of a name silently produces "" (this was a real bug: confirm_import
    used to call resolve_country again on preview's already-resolved code)."""
    return code if code in _COUNTRY_CODES else ""


def validate_language_code(code: str) -> str:
    """Same as validate_country_code but for LANGUAGE_CHOICES."""
    return code if code in _LANGUAGE_CODES else ""


def resolve_genres(names: List[str], create_missing: bool = True) -> List[Genre]:
    """create_missing=False (used by queue_import's preview) only looks up
    existing genres — it must never write to the DB before the admin has
    reviewed and confirmed an import."""
    resolved, seen_ids = [], set()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        genre = Genre.objects.filter(name__iexact=name).first()
        if genre is None:
            if not create_missing:
                continue
            genre = Genre.objects.create(name=name)
        if genre.id not in seen_ids:
            resolved.append(genre)
            seen_ids.add(genre.id)
    return resolved


def resolve_categories(names: List[str], create_missing: bool = True) -> List[Category]:
    resolved, seen_ids = [], set()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        category = Category.objects.filter(name__iexact=name).first()
        if category is None:
            if not create_missing:
                continue
            category = Category.objects.create(name=name)
        if category.id not in seen_ids:
            resolved.append(category)
            seen_ids.add(category.id)
    return resolved


def _unique_slug(model, base: str) -> str:
    # Tag/Theme both require a unique slug that Genre/Category don't have —
    # shared here since resolve_tags/resolve_themes are otherwise identical.
    base_slug = slugify(base) or model.__name__.lower()
    slug = base_slug
    index = 2
    while model.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


def resolve_tags(names: List[str], create_missing: bool = True) -> List[Tag]:
    resolved, seen_ids = [], set()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        tag = Tag.objects.filter(name__iexact=name).first()
        if tag is None:
            if not create_missing:
                continue
            tag = Tag.objects.create(name=name, slug=_unique_slug(Tag, name))
        if tag.id not in seen_ids:
            resolved.append(tag)
            seen_ids.add(tag.id)
    return resolved


def resolve_themes(names: List[str], create_missing: bool = True) -> List[Theme]:
    resolved, seen_ids = [], set()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        theme = Theme.objects.filter(name__iexact=name).first()
        if theme is None:
            if not create_missing:
                continue
            theme = Theme.objects.create(name=name, slug=_unique_slug(Theme, name))
        if theme.id not in seen_ids:
            resolved.append(theme)
            seen_ids.add(theme.id)
    return resolved


def sanitize_published_date(
    year: Optional[int], month: Optional[int], day: Optional[int]
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    # Model field validators (MinValueValidator/MaxValueValidator) are only
    # enforced by full_clean()/serializer validation, not by .create() — an
    # out-of-range value would otherwise save verbatim.
    if not year:
        return None, None, None
    if month is not None and not (1 <= month <= 12):
        month = None
    if month is None:
        return year, None, None
    if day is not None:
        try:
            date(year, month, day)
        except ValueError:
            day = None
    return year, month, day


def sanitize_url(raw: str) -> str:
    # URLValidator is likewise only enforced by full_clean(), not .create().
    value = raw.strip()
    if not value:
        return ""
    try:
        _url_validator(value)
    except DjangoValidationError:
        return ""
    return value


def dedupe_records(
    records: List[_BookRecord], story_pairs: set, queue_pairs: set
) -> Tuple[List[_BookRecord], List[Tuple[_BookRecord, DuplicateReason]]]:
    """Drops: blank titles ("missing_title"), anything matching an existing
    Story ("already_a_story") or not-yet-added StoryQueue row
    ("already_in_queue"), and duplicates within the batch itself
    ("duplicate_in_file", keeping the first occurrence). Returns
    (survivors, duplicates) — duplicates carries the reason for display."""
    survivors: List[_BookRecord] = []
    duplicates: List[Tuple[_BookRecord, DuplicateReason]] = []
    seen_in_batch = set()
    for record in records:
        if not record.title.strip():
            duplicates.append((record, "missing_title"))
            continue
        key = normalize_title_author(record.title, record.author_name)
        if key in story_pairs:
            duplicates.append((record, "already_a_story"))
            continue
        if key in queue_pairs:
            duplicates.append((record, "already_in_queue"))
            continue
        if key in seen_in_batch:
            duplicates.append((record, "duplicate_in_file"))
            continue
        seen_in_batch.add(key)
        survivors.append(record)
    return survivors, duplicates

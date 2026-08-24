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

from .book_fetch import _BookRecord
from .models import COUNTRY_CHOICES, Category, Genre, LANGUAGE_CHOICES, STORY_TYPE_CHOICES, Story, StoryQueue

_STORY_TYPE_VALUES = {value for value, _ in STORY_TYPE_CHOICES}
_COUNTRY_NAME_TO_CODE = {name.lower(): code for code, name in COUNTRY_CHOICES}
_LANGUAGE_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGE_CHOICES}
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


def resolve_story_type(raw: str) -> str:
    return raw if raw in _STORY_TYPE_VALUES else ""


def resolve_country(raw: str) -> str:
    return _COUNTRY_NAME_TO_CODE.get(raw.strip().lower(), "")


def resolve_language(raw: str) -> str:
    return _LANGUAGE_NAME_TO_CODE.get(raw.strip().lower(), "")


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

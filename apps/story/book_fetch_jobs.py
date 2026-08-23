"""Runs the Story Queue "Fetch Book Data" action off the request thread.

No task queue exists anywhere in this codebase, and production runs a
single-service, plain sync gunicorn deployment (3 workers, no Celery/Redis,
core/asgi.py unused) — a ThreadPoolExecutor is the simplest mechanism that
fits, without adding new infra for one feature. See epub_import_jobs.py's
module docstring for the full reasoning (transaction.on_commit submission,
manual connections.close_all()) — this module follows the identical shape.
"""
import csv
import io
import logging
from datetime import date
from typing import List, Optional, Tuple

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import connections, transaction

from .background_jobs import executor
from .book_fetch import BookFetchError, _BookRecord, fetch_books
from .models import (
    BookFetchJob,
    COUNTRY_CHOICES,
    Category,
    Genre,
    LANGUAGE_CHOICES,
    PromptSettings,
    STORY_TYPE_CHOICES,
    Story,
    StoryQueue,
)

logger = logging.getLogger(__name__)

_STORY_TYPE_VALUES = {value for value, _ in STORY_TYPE_CHOICES}
_COUNTRY_NAME_TO_CODE = {name.lower(): code for code, name in COUNTRY_CHOICES}
_LANGUAGE_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGE_CHOICES}
_url_validator = URLValidator()


def _existing_title_author_pairs() -> List[Tuple[str, str]]:
    """Every title this feature must never re-suggest: all Story rows (any
    status — a draft is still "already in the database") plus StoryQueue
    rows not yet turned into a Story (is_added=False — an already-added
    queue row is redundant with its Story counterpart). Same scope as
    StoryQueueViewSet.check_title's query."""
    pairs = [
        (title, author_name or "")
        for title, author_name in Story.objects.select_related("author").values_list("title", "author__name")
    ]
    pairs += list(StoryQueue.objects.filter(is_added=False).values_list("title", "author_name"))
    return pairs


def _build_existing_titles_csv(pairs: List[Tuple[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["title", "author"])
    writer.writerows(pairs)
    return buf.getvalue()


def _normalize(title: str, author: str) -> Tuple[str, str]:
    return title.strip().lower(), (author or "").strip().lower()


def _resolve_story_type(raw: str) -> str:
    return raw if raw in _STORY_TYPE_VALUES else ""


def _resolve_country(raw: str) -> str:
    return _COUNTRY_NAME_TO_CODE.get(raw.strip().lower(), "")


def _resolve_language(raw: str) -> str:
    return _LANGUAGE_NAME_TO_CODE.get(raw.strip().lower(), "")


def _resolve_genres(names: List[str]) -> List[Genre]:
    resolved, seen_ids = [], set()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        genre = Genre.objects.filter(name__iexact=name).first() or Genre.objects.create(name=name)
        if genre.id not in seen_ids:
            resolved.append(genre)
            seen_ids.add(genre.id)
    return resolved


def _resolve_categories(names: List[str]) -> List[Category]:
    resolved, seen_ids = [], set()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        category = Category.objects.filter(name__iexact=name).first() or Category.objects.create(name=name)
        if category.id not in seen_ids:
            resolved.append(category)
            seen_ids.add(category.id)
    return resolved


def _sanitize_published_date(
    year: Optional[int], month: Optional[int], day: Optional[int]
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    # Model field validators (MinValueValidator/MaxValueValidator) are only
    # enforced by full_clean()/serializer validation, not by .create() — an
    # out-of-range value from Claude would otherwise save verbatim.
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


def _sanitize_url(raw: str) -> str:
    # URLValidator is likewise only enforced by full_clean(), not .create().
    value = raw.strip()
    if not value:
        return ""
    try:
        _url_validator(value)
    except DjangoValidationError:
        return ""
    return value


def _dedupe_records(records: List[_BookRecord], existing_pairs: set) -> Tuple[List[_BookRecord], int]:
    """Drops: blank titles, anything matching Story/StoryQueue (the same
    set the CSV was built from), and duplicates within Claude's own batch
    (keeping the first occurrence). Everything dropped counts toward
    skipped_count."""
    survivors: List[_BookRecord] = []
    seen_in_batch = set()
    skipped = 0
    for record in records:
        if not record.title.strip():
            skipped += 1
            continue
        key = _normalize(record.title, record.author_name)
        if key in existing_pairs or key in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(key)
        survivors.append(record)
    return survivors, skipped


def run_book_fetch(job_id: int) -> None:
    """Entry point submitted to `executor`. Runs in a worker thread — opens
    its own DB connection, must close it before returning (see module
    docstring)."""
    try:
        job = BookFetchJob.objects.get(pk=job_id)
        job.status = BookFetchJob.STATUS_PROCESSING
        job.save(update_fields=["status", "updated_at"])

        try:
            existing_pairs_raw = _existing_title_author_pairs()
            existing_titles_csv = _build_existing_titles_csv(existing_pairs_raw)
            existing_pairs = {_normalize(title, author) for title, author in existing_pairs_raw}

            prompt_settings = PromptSettings.get_solo()
            records = fetch_books(
                existing_titles_csv=existing_titles_csv,
                count=job.requested_count,
                instructions=prompt_settings.book_fetch_instructions,
                model=prompt_settings.book_fetch_model,
            )

            survivors, skipped = _dedupe_records(records, existing_pairs)

            created_count = 0
            with transaction.atomic():
                for record in survivors:
                    year, month, day = _sanitize_published_date(
                        record.original_published_year,
                        record.original_published_month,
                        record.original_published_day,
                    )
                    queue_item = StoryQueue.objects.create(
                        title=record.title.strip(),
                        author_name=record.author_name.strip(),
                        about=record.about.strip() or None,
                        story_type=_resolve_story_type(record.story_type),
                        country=_resolve_country(record.country),
                        language=_resolve_language(record.language),
                        original_published_year=year,
                        original_published_month=month,
                        original_published_day=day,
                        epub_link=_sanitize_url(record.epub_link),
                        pdf_link=_sanitize_url(record.pdf_link),
                        cover_image_link=_sanitize_url(record.cover_image_link),
                    )
                    queue_item.genres.set(_resolve_genres(record.genres))
                    queue_item.categories.set(_resolve_categories(record.categories))
                    created_count += 1

            job.status = BookFetchJob.STATUS_COMPLETED
            job.created_count = created_count
            job.skipped_count = skipped
            job.error_message = None
            job.save(update_fields=["status", "created_count", "skipped_count", "error_message", "updated_at"])
        except BookFetchError as exc:
            job.status = BookFetchJob.STATUS_FAILED
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message", "updated_at"])
            logger.warning("Book fetch failed: job_id=%s error=%s", job_id, exc)
        except Exception:
            job.status = BookFetchJob.STATUS_FAILED
            job.error_message = "Unexpected internal error."
            job.save(update_fields=["status", "error_message", "updated_at"])
            logger.exception("Book fetch failed unexpectedly: job_id=%s", job_id)
    except Exception:
        logger.exception("Book fetch job %s could not even be started/recorded", job_id)
    finally:
        connections.close_all()

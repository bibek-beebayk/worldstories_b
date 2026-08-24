"""Runs the Story Queue "Fetch Book Data" action off the request thread.

No task queue exists anywhere in this codebase, and production runs a
single-service, plain sync gunicorn deployment (3 workers, no Celery/Redis,
core/asgi.py unused) — a ThreadPoolExecutor is the simplest mechanism that
fits, without adding new infra for one feature. See epub_import_jobs.py's
module docstring for the full reasoning (transaction.on_commit submission,
manual connections.close_all()) — this module follows the identical shape.

Candidate-record resolution/dedup (resolve_story_type/country/language/
genres/categories, sanitize_*, dedupe_records) lives in queue_records.py,
shared with queue_import.py's CSV/Excel import path — both sources of
StoryQueue candidates must apply identical rules.
"""
import csv
import io
import logging
from typing import List, Tuple

from django.db import connections, transaction

from .background_jobs import executor
from .book_fetch import BookFetchError, fetch_books
from .models import BookFetchJob, PromptSettings, StoryQueue
from .queue_records import (
    dedupe_records,
    existing_queue_normalized_pairs,
    existing_story_normalized_pairs,
    existing_title_author_pairs,
    resolve_categories,
    resolve_country,
    resolve_genres,
    resolve_language,
    resolve_story_type,
    sanitize_published_date,
    sanitize_url,
)

logger = logging.getLogger(__name__)


def _build_existing_titles_csv(pairs: List[Tuple[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["title", "author"])
    writer.writerows(pairs)
    return buf.getvalue()


def run_book_fetch(job_id: int) -> None:
    """Entry point submitted to `executor`. Runs in a worker thread — opens
    its own DB connection, must close it before returning (see module
    docstring)."""
    try:
        job = BookFetchJob.objects.get(pk=job_id)
        job.status = BookFetchJob.STATUS_PROCESSING
        job.save(update_fields=["status", "updated_at"])

        try:
            existing_pairs_raw = existing_title_author_pairs()
            existing_titles_csv = _build_existing_titles_csv(existing_pairs_raw)

            prompt_settings = PromptSettings.get_solo()
            records = fetch_books(
                existing_titles_csv=existing_titles_csv,
                count=job.requested_count,
                instructions=prompt_settings.book_fetch_instructions,
                model=prompt_settings.book_fetch_model,
            )

            survivors, duplicates = dedupe_records(
                records, existing_story_normalized_pairs(), existing_queue_normalized_pairs()
            )

            created_count = 0
            with transaction.atomic():
                for record in survivors:
                    year, month, day = sanitize_published_date(
                        record.original_published_year,
                        record.original_published_month,
                        record.original_published_day,
                    )
                    queue_item = StoryQueue.objects.create(
                        title=record.title.strip(),
                        author_name=record.author_name.strip(),
                        about=record.about.strip() or None,
                        story_type=resolve_story_type(record.story_type),
                        country=resolve_country(record.country),
                        language=resolve_language(record.language),
                        original_published_year=year,
                        original_published_month=month,
                        original_published_day=day,
                        epub_link=sanitize_url(record.epub_link),
                        pdf_link=sanitize_url(record.pdf_link),
                        cover_image_link=sanitize_url(record.cover_image_link),
                    )
                    queue_item.genres.set(resolve_genres(record.genres))
                    queue_item.categories.set(resolve_categories(record.categories))
                    created_count += 1

            job.status = BookFetchJob.STATUS_COMPLETED
            job.created_count = created_count
            job.skipped_count = len(duplicates)
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

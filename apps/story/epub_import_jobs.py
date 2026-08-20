"""Runs EPUB chapter extraction (epub_import.extract_chapters) off the
request thread.

No task queue exists anywhere in this codebase, and production runs a
single-service, plain sync gunicorn deployment (3 workers, no Celery/Redis,
core/asgi.py unused) — a ThreadPoolExecutor is the simplest mechanism that
fits, without adding new infra for one feature.

ATOMIC_REQUESTS = True (core/settings/base.py) wraps every view in its own
transaction, so a job must never be submitted to the executor until the view's
transaction has actually committed — otherwise the worker thread's own
connection can race the still-open request transaction (reading stale state,
or contending for locks the request itself still holds). See import_epub in
api.py, which submits via transaction.on_commit(...), never directly.

The worker thread also gets its own DB connection the moment it touches the
ORM — Django's usual per-request connection cleanup is signal-driven and
never fires for a thread that isn't a request, so it must be closed manually
in the finally block below or it leaks for the life of the thread.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from django.db import connections, transaction
from django.utils.text import slugify

from .epub_import import EpubParseError, extract_chapters
from .models import Chapter, EpubImportJob

logger = logging.getLogger(__name__)

# Small on purpose: this is I/O (remote-storage fetch) plus moderate CPU
# parsing work, not meant to compete with gunicorn's own worker processes
# for cores. Module-level so it's shared/reused across requests within one
# process rather than spun up per-request.
executor = ThreadPoolExecutor(max_workers=2)


def _build_unique_chapter_slug(story, title: str) -> str:
    # Mirrors ChapterAdminSerializer._build_unique_slug (serializers.py) —
    # duplicated rather than imported since that one is scoped to a single
    # (possibly pre-existing) instance being excluded from the collision
    # check, which doesn't apply here (all prior chapters for this story
    # were just deleted before this runs).
    base_slug = slugify(title) or "chapter"
    slug = base_slug
    suffix = 2
    existing = set(Chapter.objects.filter(story=story).values_list("slug", flat=True))
    while slug in existing:
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


def run_epub_import(job_id: int) -> None:
    """Entry point submitted to `executor`. Runs in a worker thread — opens
    its own DB connection, must close it before returning (see module
    docstring)."""
    try:
        job = EpubImportJob.objects.select_related("story").get(pk=job_id)
        job.status = EpubImportJob.STATUS_PROCESSING
        job.save(update_fields=["status", "updated_at"])

        story = job.story
        try:
            with story.epub_file.open("rb") as fileobj:
                epub_bytes = fileobj.read()

            chapters_data = extract_chapters(epub_bytes)

            with transaction.atomic():
                # Re-import replaces rather than appends — running import a
                # second time on the same story is expected to reflect
                # whatever epub_file currently points at, not accumulate
                # duplicates alongside hand-edited chapters.
                story.chapters.all().delete()
                for chapter_data in chapters_data:
                    Chapter.objects.create(
                        story=story,
                        title=chapter_data.title,
                        slug=_build_unique_chapter_slug(story, chapter_data.title),
                        content=chapter_data.content_html,
                        order=chapter_data.order,
                    )

            job.status = EpubImportJob.STATUS_COMPLETED
            job.chapters_created = len(chapters_data)
            job.error_message = None
            job.save(update_fields=["status", "chapters_created", "error_message", "updated_at"])
        except EpubParseError as exc:
            job.status = EpubImportJob.STATUS_FAILED
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message", "updated_at"])
            logger.warning(
                "EPUB import failed to parse: story_id=%s job_id=%s filename=%s error=%s",
                story.id, job.id, story.epub_file.name, exc,
            )
        except Exception as exc:
            job.status = EpubImportJob.STATUS_FAILED
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message", "updated_at"])
            logger.exception(
                "EPUB import failed unexpectedly: story_id=%s job_id=%s filename=%s",
                story.id, job.id, story.epub_file.name,
            )
    except Exception:
        logger.exception("EPUB import job %s could not even be started/recorded", job_id)
    finally:
        connections.close_all()

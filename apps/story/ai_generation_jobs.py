"""Runs Claude summary/retrospective generation (ai_generation.generate) off
the request thread. See epub_import_jobs.py's module docstring for why a
ThreadPoolExecutor is used and why the worker thread must close its own DB
connection when done.
"""
import logging

from django.db import connections
from django.utils.html import strip_tags

from .ai_generation import MAX_CONTENT_CHARS, GenerationError, generate
from .background_jobs import executor
from .models import PromptSettings, Story

logger = logging.getLogger(__name__)


def _concatenated_chapter_text(story: Story) -> str:
    """All of story.chapters' content, in Chapter.Meta.ordering ("order")
    order, HTML-stripped to plain text and capped at MAX_CONTENT_CHARS. Never
    modifies Chapter.content — this text is ephemeral API input only."""
    parts = []
    total = 0
    for chapter in story.chapters.all():
        text = strip_tags(chapter.content or "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= MAX_CONTENT_CHARS:
            break
    return "\n\n".join(parts)[:MAX_CONTENT_CHARS]


def run_generate_field(story_id: int, action: str, input_fields: list) -> None:
    """Entry point submitted to executor. action is "summary" or
    "retrospective"; writes only that action's own {action}/_status/_source/
    _confident/_confidence_note/_error columns via a targeted UPDATE (not
    story.save()) so a concurrent run for the *other* action on the same
    story can't clobber these columns, or have its own columns clobbered."""
    status_field = f"{action}_status"
    source_field = f"{action}_source"
    confident_field = f"{action}_confident"
    note_field = f"{action}_confidence_note"
    error_field = f"{action}_error"
    try:
        story = Story.objects.select_related("author").get(pk=story_id)
        Story.objects.filter(pk=story_id).update(**{status_field: Story.GEN_STATUS_PROCESSING})

        try:
            prompt_settings = PromptSettings.get_solo()
            instructions = (
                prompt_settings.summary_instructions
                if action == "summary"
                else prompt_settings.retrospective_instructions
            )
            model = prompt_settings.summary_model if action == "summary" else prompt_settings.retrospective_model
            content_text = _concatenated_chapter_text(story) if "content" in input_fields else None

            result = generate(
                action=action,
                instructions=instructions,
                title=story.title,
                author_name=story.author.name if story.author else None,
                input_fields=input_fields,
                model=model,
                content_text=content_text,
            )

            Story.objects.filter(pk=story_id).update(**{
                action: result.html,
                status_field: Story.GEN_STATUS_COMPLETED,
                source_field: result.source,
                confident_field: result.confident,
                note_field: result.confidence_note,
                error_field: None,
            })
        except GenerationError as exc:
            Story.objects.filter(pk=story_id).update(**{status_field: Story.GEN_STATUS_FAILED, error_field: str(exc)})
            logger.warning("%s generation failed: story_id=%s error=%s", action, story_id, exc)
        except Exception:
            Story.objects.filter(pk=story_id).update(
                **{status_field: Story.GEN_STATUS_FAILED, error_field: "Unexpected internal error."}
            )
            logger.exception("%s generation failed unexpectedly: story_id=%s", action, story_id)
    except Exception:
        logger.exception("ai_generation job could not even be started/recorded: story_id=%s action=%s", story_id, action)
    finally:
        connections.close_all()

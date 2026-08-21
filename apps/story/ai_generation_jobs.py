"""Runs Claude summary/retrospective/excerpt generation (ai_generation.generate)
off the request thread. See epub_import_jobs.py's module docstring for why a
ThreadPoolExecutor is used and why the worker thread must close its own DB
connection when done.
"""
import logging

from django.db import connections
from django.utils.html import strip_tags

from .ai_generation import MAX_CONTENT_CHARS, GenerationError, generate
from .background_jobs import executor
from .models import Blog, PromptSettings, Story

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
                action: result.content,
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


def _plain_blog_content(blog: Blog) -> str:
    """blog.content HTML-stripped to plain text and capped at
    MAX_CONTENT_CHARS. Never modifies Blog.content — ephemeral API input
    only, same reasoning as _concatenated_chapter_text above."""
    text = strip_tags(blog.content or "").strip()
    return text[:MAX_CONTENT_CHARS]


def run_generate_blog_excerpt(blog_id: int) -> None:
    """Entry point submitted to executor for Blog.excerpt generation. Unlike
    Story summary/retrospective (which may cover a well-known book Claude
    already has general knowledge of), a blog post has no such fallback —
    the excerpt is always grounded in the post's own content. Always renders
    plain text (render_as="text") — Blog.excerpt is a CharField(300), not a
    rich-text field."""
    try:
        blog = Blog.objects.get(pk=blog_id)
        Blog.objects.filter(pk=blog_id).update(excerpt_status=Blog.GEN_STATUS_PROCESSING)

        try:
            prompt_settings = PromptSettings.get_solo()

            result = generate(
                action="excerpt",
                instructions=prompt_settings.excerpt_instructions,
                title=blog.title,
                author_name=blog.author_name,
                input_fields=["title", "author", "content"],
                model=prompt_settings.excerpt_model,
                content_text=_plain_blog_content(blog),
                render_as="text",
            )

            Blog.objects.filter(pk=blog_id).update(
                excerpt=result.content,
                excerpt_status=Blog.GEN_STATUS_COMPLETED,
                excerpt_source=result.source,
                excerpt_confident=result.confident,
                excerpt_confidence_note=result.confidence_note,
                excerpt_error=None,
            )
        except GenerationError as exc:
            Blog.objects.filter(pk=blog_id).update(excerpt_status=Blog.GEN_STATUS_FAILED, excerpt_error=str(exc))
            logger.warning("excerpt generation failed: blog_id=%s error=%s", blog_id, exc)
        except Exception:
            Blog.objects.filter(pk=blog_id).update(
                excerpt_status=Blog.GEN_STATUS_FAILED, excerpt_error="Unexpected internal error."
            )
            logger.exception("excerpt generation failed unexpectedly: blog_id=%s", blog_id)
    except Exception:
        logger.exception("ai_generation job could not even be started/recorded: blog_id=%s action=excerpt", blog_id)
    finally:
        connections.close_all()

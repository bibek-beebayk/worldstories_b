"""Builds Claude prompts, calls the Anthropic Messages API, and parses/
validates the structured response for book-level Summary/Retrospective
generation. No Django ORM/DB access — operates only on already-extracted
strings (title, author name, concatenated chapter text) so it's directly
unit-testable and reusable from both a live request and a background thread
(see ai_generation_jobs.py for the DB-touching side of the pipeline).
"""
import logging
from dataclasses import dataclass
from typing import Literal, Optional

import anthropic
import markdown
import nh3
from django.conf import settings
from pydantic import BaseModel

from .epub_import import ALLOWED_ATTRIBUTES, ALLOWED_TAGS

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-5"

# Cost/latency cap on concatenated chapter text sent per API call.
MAX_CONTENT_CHARS = 60_000


class _GenerationOutput(BaseModel):
    text: str
    confident: bool
    confidence_note: str


@dataclass
class GenerationResult:
    html: str
    source: Literal["metadata", "content"]
    confident: bool
    confidence_note: str


class GenerationError(Exception):
    """Every attempt (the same-mode retry, and — for metadata mode — the
    content-mode fallback) failed with an API error or invalid response.
    Callers (ai_generation_jobs.py) catch this to mark the field failed."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _build_user_message(
    action: str, title: str, author_name: Optional[str], content_text: Optional[str]
) -> str:
    # Mechanical assembly only — title/author/content are placed into a
    # fixed template; the admin-editable portion (PromptSettings instructions)
    # is passed separately as the system prompt and never interpolated into
    # this string, so admin-authored text can't reshape this framing.
    lines = [f"Title: {title}", f"Author: {author_name or 'Unknown'}"]
    if content_text is not None:
        lines.append(f"\nFull book text (may be truncated):\n{content_text}")
    else:
        lines.append(
            "\n(No chapter text provided — base this only on the title and "
            "author above. Set confident=false if you do not have specific, "
            "concrete knowledge of this exact book.)"
        )
    lines.append(
        f"\nWrite the {action} described in the system instructions. Return "
        "it as Markdown (paragraphs, and **bold**/*italics*/lists/headings/"
        "blockquotes where they genuinely help — not HTML)."
    )
    return "\n".join(lines)


def _to_html(raw_text: str) -> str:
    # Claude is instructed to return Markdown — converted here to the same
    # rich-text HTML the CKEditor5 summary/retrospective fields already
    # store, then sanitized regardless of what it actually returns (Markdown
    # passes raw HTML blocks through unchanged by design, so this is the
    # real security boundary, not a redundant step).
    html = markdown.markdown(raw_text, extensions=["extra"])
    return nh3.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, link_rel=None)


def _call_once(
    instructions: str,
    action: str,
    title: str,
    author_name: Optional[str],
    content_text: Optional[str],
    model: str,
) -> GenerationResult:
    """Single Anthropic API call + parse. Raises GenerationError on API
    failure or an invalid/empty response — callers own retry/fallback."""
    try:
        response = _client().messages.parse(
            model=model,
            max_tokens=4096,
            system=instructions,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_message(action, title, author_name, content_text),
                }
            ],
            output_format=_GenerationOutput,
        )
    except anthropic.NotFoundError as exc:
        raise GenerationError(f"Invalid model or endpoint: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise GenerationError(f"Rate limited: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise GenerationError(f"Anthropic API error ({exc.status_code}): {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise GenerationError(f"Network error calling Anthropic: {exc}") from exc

    data = response.parsed_output
    if data is None or not data.text.strip():
        raise GenerationError("Response was empty or failed schema validation.")

    source = "content" if content_text is not None else "metadata"
    return GenerationResult(
        html=_to_html(data.text),
        source=source,
        confident=data.confident,
        confidence_note=data.confidence_note,
    )


def generate(
    action: str,
    instructions: str,
    title: str,
    author_name: Optional[str],
    input_fields: list,
    content_text: Optional[str],
    model: str = MODEL_ID,
) -> GenerationResult:
    """Orchestrates: no auto-retry on low confidence — a confident=False
    result is returned as-is, once; one same-mode retry on hard failure
    (API error / invalid response); and — only for metadata-mode requests —
    one additional fallback to content mode if metadata mode still failed
    after its retry. Content-mode failures are not retried further (nothing
    left to fall back to). Raises GenerationError only if every attempt
    failed. `model` is admin-selected per action (PromptSettings.summary_model
    / retrospective_model), defaulting to MODEL_ID only for direct/test calls.
    """
    requested_content_text = content_text if "content" in input_fields else None
    errors = []
    for attempt in range(2):  # same-mode: try, then one retry
        try:
            return _call_once(instructions, action, title, author_name, requested_content_text, model)
        except GenerationError as exc:
            errors.append(str(exc))
            logger.warning(
                "ai_generation attempt %s (%s mode) failed: %s",
                attempt + 1,
                "content" if requested_content_text is not None else "metadata",
                exc,
            )

    if requested_content_text is None and content_text is not None:
        # Metadata mode exhausted its retry; auto-fallback to content mode
        # once, since we have chapter text available to fall back to.
        logger.info("ai_generation falling back from metadata to content mode after repeated hard failures.")
        try:
            return _call_once(instructions, action, title, author_name, content_text, model)
        except GenerationError as exc:
            errors.append(str(exc))

    raise GenerationError("; ".join(errors))

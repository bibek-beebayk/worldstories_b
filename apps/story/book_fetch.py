"""Builds the Claude prompt, calls the Anthropic Messages API, and parses/
validates the structured response for the Story Queue "Fetch Book Data"
admin action. No Django ORM/DB access — operates only on an already-built
CSV string, so it's directly unit-testable and reusable from both a live
request and a background thread (see book_fetch_jobs.py for the DB-touching
side: CSV assembly, dedup, and StoryQueue row creation).
"""
import logging
from typing import List, Optional

import anthropic
from django.conf import settings
from pydantic import BaseModel, ValidationError as PydanticValidationError

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-5"

DEFAULT_BOOK_FETCH_COUNT = 10
# Keeps the Claude call safely non-streaming — max_tokens never needs to
# exceed what a single non-streaming response can reliably return.
MAX_BOOK_FETCH_COUNT = 14


class _BookRecord(BaseModel):
    title: str
    author_name: str
    about: str
    story_type: str
    country: str
    language: str
    genres: List[str]
    categories: List[str]
    original_published_year: Optional[int] = None
    original_published_month: Optional[int] = None
    original_published_day: Optional[int] = None
    epub_link: str
    pdf_link: str
    cover_image_link: str


class _BookFetchOutput(BaseModel):
    books: List[_BookRecord]


class BookFetchError(Exception):
    """API failure or an empty/invalid response. book_fetch_jobs.run_book_fetch
    catches this to mark the job failed. No retry loop here (unlike
    ai_generation.generate) — a failed batch is cheap to re-trigger from the
    admin UI, so this keeps the failure mode to one call, one outcome."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _build_user_message(existing_titles_csv: str, count: int, story_type_names: List[str]) -> str:
    # Mechanical assembly only — the CSV and count are placed into a fixed
    # template; the admin-editable portion (PromptSettings.book_fetch_instructions)
    # is passed separately as the system prompt and never interpolated into
    # this string, matching ai_generation._build_user_message's same split.
    # story_type_names comes from the live StoryType table (book_fetch_jobs.py
    # — this module has no DB access of its own) rather than a hardcoded list,
    # so newly admin-created types are usable immediately.
    story_types = ", ".join(story_type_names)
    return (
        "Existing titles already in our catalog — do not suggest any of these "
        "(a book counts as a duplicate if both its title and author match a "
        "row below, case-insensitively):\n\n"
        f"```csv\n{existing_titles_csv}```\n\n"
        f"Suggest exactly {count} public-domain books not in the list above. "
        f"If you cannot find {count} that genuinely meet the public-domain and "
        "quality bar, return fewer rather than padding with weak or uncertain "
        "suggestions.\n\n"
        "For each book, return:\n"
        "- title (required)\n"
        "- author_name: empty string if unknown or anonymous\n"
        "- about: an 80-100 word SEO-friendly, non-spoiler synopsis for readers "
        "browsing a library site; empty string if you're not confident\n"
        f"- story_type: exactly one of [{story_types}], or empty string if none fit\n"
        "- country: the book's/author's country of origin as a full English "
        "country name (e.g. \"Japan\", \"France\") — not an abbreviation or "
        "code — or empty string if unknown\n"
        "- language: the language the book's text is actually in, as a full "
        "English language name (e.g. \"English\", \"Spanish\") — not a code — "
        "or empty string if unknown\n"
        "- genres: 0-4 genre names (e.g. \"Adventure\", \"Gothic\")\n"
        "- categories: 0-3 broad category names (e.g. \"Classic Literature\")\n"
        "- original_published_year/month/day: only the parts you actually know; "
        "omit the rest (leave null)\n"
        "- epub_link / pdf_link / cover_image_link: only a genuine, "
        "currently-working public-domain source for that exact edition — leave "
        "each blank unless you're genuinely confident, per the system "
        "instructions"
    )


def _max_tokens_for(count: int) -> int:
    """Generous per-record budget — a full multi-field record (80-100 word
    synopsis, three link fields, genres/categories, etc.) runs meaningfully
    higher than plain text alone once JSON structure/escaping is counted, and
    a response that runs out of budget mid-record comes back as truncated,
    invalid JSON (caught as BookFetchError below) rather than a clean error,
    so this errs generous. Capped at 20,000 — anthropic's Python SDK requires
    streaming above ~21,333 tokens for claude-sonnet-5 (no per-model
    non-streaming cap is registered for it, only the sliding time-based one:
    see anthropic._base_client.BaseClient._calculate_nonstreaming_timeout),
    so this stays comfortably under that without switching this call to
    streaming."""
    return min(20_000, 3_000 + count * 1_500)


def fetch_books(
    existing_titles_csv: str,
    count: int,
    instructions: str,
    story_type_names: List[str],
    model: str = MODEL_ID,
) -> List[_BookRecord]:
    """Single Anthropic API call + parse. Raises BookFetchError on API
    failure or an invalid/empty response — callers own recording the
    failure (book_fetch_jobs.run_book_fetch)."""
    try:
        response = _client().messages.parse(
            model=model,
            max_tokens=_max_tokens_for(count),
            system=instructions,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_message(existing_titles_csv, count, story_type_names),
                }
            ],
            output_format=_BookFetchOutput,
        )
    except anthropic.NotFoundError as exc:
        raise BookFetchError(f"Invalid model or endpoint: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise BookFetchError(f"Rate limited: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise BookFetchError(f"Anthropic API error ({exc.status_code}): {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise BookFetchError(f"Network error calling Anthropic: {exc}") from exc
    except PydanticValidationError as exc:
        # The SDK's own messages.parse() raises this directly (not wrapped
        # in an anthropic.* exception) when the response text isn't valid
        # JSON for the schema — in practice this means the response was cut
        # off mid-generation because it hit max_tokens before finishing.
        # Requesting fewer books at once is the actionable fix.
        raise BookFetchError(
            f"Claude's response was truncated or malformed (try a smaller count): {exc}"
        ) from exc

    data = response.parsed_output
    if data is None or not data.books:
        raise BookFetchError("Response was empty or failed schema validation.")
    return data.books

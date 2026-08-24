import html
import re

import nh3
from django.core.cache import cache

EXCERPT_WORD_COUNT = 20
EXCERPT_CACHE_SECONDS = 60 * 5


def _excerpt_from_text(text: str, progress: float, word_count: int = EXCERPT_WORD_COUNT) -> str:
    """Pure slicing logic, kept separate from HTML/cache handling so it's
    directly testable with plain strings. Slices `text` starting at the
    character offset implied by `progress` (0.0-1.0), snapped forward to
    the next word boundary, taking up to `word_count` words."""
    if not text:
        return ""

    progress = max(0.0, min(1.0, progress))
    offset = int(len(text) * progress)
    while 0 < offset < len(text) and not text[offset - 1].isspace():
        offset += 1
    offset = min(offset, len(text))

    words = text[offset:].split()
    if not words:
        tail_words = text.split()[-word_count:]
        return f"…{' '.join(tail_words)}" if tail_words else ""

    snippet = " ".join(words[:word_count])
    prefix = "…" if offset > 0 else ""
    suffix = "…" if len(words) > word_count else ""
    return f"{prefix}{snippet}{suffix}"


def _plain_text_for_chapter(chapter) -> str:
    cache_key = f"chapter-plaintext-{chapter.id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    text = nh3.clean(chapter.content or "", tags=set(), attributes={})
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    cache.set(cache_key, text, EXCERPT_CACHE_SECONDS)
    return text


def excerpt_at_progress(chapter, progress: float) -> str:
    """"...where you left off" — a short plain-text snippet from a
    chapter's content, positioned at the given reading-progress fraction.
    There's no real "last scrolled-to element" signal available yet
    (ReadingProgress.last_element_id is currently always empty — the
    reader never populates it), so progress is the most reliable position
    signal we have."""
    return _excerpt_from_text(_plain_text_for_chapter(chapter), progress)


def excerpt_at_query(chapter, query: str) -> str:
    """Short plain-text snippet from a chapter's content, centered on the
    first occurrence of `query` — used by site search to show why a chapter
    matched. Reuses the same word-boundary/word-count/ellipsis slicing as
    excerpt_at_progress by converting the match's character offset into a
    progress fraction. Falls back to the start of the chapter if `query`
    isn't actually in the content (e.g. it matched the chapter title
    instead, not the body text)."""
    text = _plain_text_for_chapter(chapter)
    if not text:
        return ""
    match_offset = text.lower().find(query.lower()) if query else -1
    progress = match_offset / len(text) if match_offset >= 0 else 0.0
    return _excerpt_from_text(text, progress)

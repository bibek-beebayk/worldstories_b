"""Estimates for how long a story takes to read or listen to.

Chapter-based reading time is computed live per request — chapter content
lives in Postgres already, no extra I/O. Everything else (audio duration,
epub/pdf-derived reading time) requires the actual file bytes, which for
remote storage (S3/R2) means a network fetch — too slow and too risky to do
live on every request, so those are probed once at upload time and cached
on the model instead (Audio.duration_seconds, Story.cached_file_reading_minutes
— see AudioAdminSerializer / StoryAdminSerializer). A stalled connection
to remote storage inside a live request can hang the worker handling it —
with a single sync gunicorn worker and no request timeout override, that
takes the whole site down, not just the one request; see the incident that
prompted this module's *_from_bytes() functions, which never touch storage.
"""
import io
import re

WORDS_PER_MINUTE = 200
SUMMARY_WORDS_PER_MINUTE = 220
PDF_MINUTES_PER_PAGE = 2


def read_local_upload_bytes(uploaded_file):
    """Reads an in-request upload (not yet saved to storage) fully into
    memory and rewinds it, so callers can inspect the bytes without
    disturbing the file for whatever real save follows. Never touches
    remote storage — safe to call from inside a live request."""
    if not uploaded_file:
        return None
    try:
        uploaded_file.seek(0)
        raw_bytes = uploaded_file.read()
        uploaded_file.seek(0)
        return raw_bytes
    except Exception:
        return None


def _word_count_from_html(raw_html):
    if not raw_html:
        return 0
    try:
        from lxml import html as lxml_html

        text = lxml_html.fromstring(raw_html).text_content()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw_html)
    return len(text.split())


def _minutes_from_word_count(word_count):
    if word_count <= 0:
        return None
    return max(1, round(word_count / WORDS_PER_MINUTE))


def chapters_reading_minutes(story):
    total_words = sum(
        _word_count_from_html(content)
        for content in story.chapters.values_list("content", flat=True)
    )
    return _minutes_from_word_count(total_words)


def summary_reading_minutes(summary_html):
    """Quick Read reading-time estimate — summaries read faster than full
    prose, so this uses a higher words-per-minute rate than the general
    WORDS_PER_MINUTE above. Mirrors the frontend's estimateSummaryReadingMinutes
    (src/lib/summaryReadingTime.ts), which is the source of truth on a story's
    own page/Quick Read page; this server-side copy exists only for the
    homepage Quick Read section (StoryListSerializer), which needs the
    estimate without shipping the full summary text in a list response."""
    word_count = _word_count_from_html(summary_html)
    if word_count <= 0:
        return None
    return max(1, round(word_count / SUMMARY_WORDS_PER_MINUTE))


def epub_minutes_from_bytes(raw_bytes):
    """Parses an epub already available locally — no I/O of its own. Use
    this (via a local upload, not epub_file) for anything running inside a
    live request; see epub_reading_minutes below."""
    try:
        from ebooklib import epub, ITEM_DOCUMENT
    except ImportError:
        return None

    try:
        book = epub.read_epub(io.BytesIO(raw_bytes))
    except Exception:
        return None

    total_words = 0
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        total_words += _word_count_from_html(item.get_content().decode("utf-8", errors="ignore"))
    return _minutes_from_word_count(total_words)


def epub_reading_minutes(epub_file):
    """Opens a *saved* Story.epub_file, which for remote storage means a
    live network fetch of the whole file. Fine for an offline job like
    backfill_file_reading_times, but a live request on a freshly-uploaded
    file should read the local upload directly and call
    epub_minutes_from_bytes instead (see StoryAdminSerializer) — same
    reasoning as probe_audio_duration_seconds above."""
    try:
        with epub_file.open("rb") as fileobj:
            raw_bytes = fileobj.read()
    except Exception:
        return None

    return epub_minutes_from_bytes(raw_bytes)


def pdf_minutes_from_bytes(raw_bytes):
    """Parses a PDF already available locally — no I/O of its own. Use this
    (via a local upload, not pdf_file) for anything running inside a live
    request; see pdf_reading_minutes below."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    try:
        page_count = len(PdfReader(io.BytesIO(raw_bytes)).pages)
    except Exception:
        return None

    if page_count <= 0:
        return None
    return max(1, round(page_count * PDF_MINUTES_PER_PAGE))


def pdf_reading_minutes(pdf_file):
    """Opens a *saved* Story.pdf_file — see epub_reading_minutes above for
    why this should only be used offline (backfill_file_reading_times), never
    from a live request."""
    try:
        with pdf_file.open("rb") as fileobj:
            raw_bytes = fileobj.read()
    except Exception:
        return None

    return pdf_minutes_from_bytes(raw_bytes)


def story_reading_minutes(story):
    """chapters (computed live, cheap) > cached epub/pdf-derived estimate —
    same priority as which reader the "Start Reading" button opens. The
    file-based estimate is computed once, at upload time (StoryAdminSerializer)
    or via the backfill_file_reading_times management command, and cached on
    cached_file_reading_minutes — never parsed live from remote storage on a
    story-detail page view."""
    if story.chapters.exists():
        return chapters_reading_minutes(story)
    return story.cached_file_reading_minutes


def _duration_from_mutagen_info(info):
    if info is None or not info.info or not info.info.length:
        return None
    return float(info.info.length)


def probe_audio_duration_from_bytes(raw_bytes):
    """Probes duration from bytes already available locally — no I/O of its
    own. Use this (via a local UploadedFile, not audio_file) for anything
    running inside a live request; see probe_audio_duration_seconds below."""
    try:
        import mutagen
    except ImportError:
        return None

    try:
        info = mutagen.File(io.BytesIO(raw_bytes))
    except Exception:
        return None

    return _duration_from_mutagen_info(info)


def probe_audio_duration_seconds(audio_file):
    """Opens a *saved* Audio.audio_file, which for remote storage (S3/R2)
    means a live network round-trip to fetch the whole file. Fine for an
    offline job like backfill_audio_durations, but never call this from a
    live request on a freshly-uploaded file — AudioAdminSerializer already
    has the bytes locally pre-upload and should probe those directly with
    probe_audio_duration_from_bytes instead, or a stalled/slow connection to
    remote storage hangs the request (and, with a single sync gunicorn
    worker and no request timeout override, the entire site) until gunicorn's
    worker-timeout kills it."""
    try:
        with audio_file.open("rb") as fileobj:
            raw_bytes = fileobj.read()
    except Exception:
        return None

    return probe_audio_duration_from_bytes(raw_bytes)


def story_listening_minutes(audios):
    durations = [audio.duration_seconds for audio in audios if audio.duration_seconds]
    if not durations:
        return None
    return max(1, round(sum(durations) / 60))

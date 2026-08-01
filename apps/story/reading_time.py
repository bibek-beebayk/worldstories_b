"""Estimates for how long a story takes to read or listen to.

Reading time is computed live per request: chapter content lives in Postgres
already (no extra I/O), and epub/pdf files for these short-form stories are
small enough that parsing them on demand is cheap. Audio duration is the
exception — probing it requires the actual audio bytes, which would mean
downloading full audio files from remote storage on every story-detail page
view, so that value is probed once at upload time and cached on the Audio
model instead (see AudioAdminSerializer).
"""
import io
import re

WORDS_PER_MINUTE = 200
PDF_MINUTES_PER_PAGE = 2


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


def epub_reading_minutes(epub_file):
    try:
        from ebooklib import epub, ITEM_DOCUMENT
    except ImportError:
        return None

    try:
        with epub_file.open("rb") as fileobj:
            book = epub.read_epub(io.BytesIO(fileobj.read()))
    except Exception:
        return None

    total_words = 0
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        total_words += _word_count_from_html(item.get_content().decode("utf-8", errors="ignore"))
    return _minutes_from_word_count(total_words)


def pdf_reading_minutes(pdf_file):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    try:
        with pdf_file.open("rb") as fileobj:
            page_count = len(PdfReader(io.BytesIO(fileobj.read())).pages)
    except Exception:
        return None

    if page_count <= 0:
        return None
    return max(1, round(page_count * PDF_MINUTES_PER_PAGE))


def story_reading_minutes(story):
    """chapters > epub > pdf — same priority as which reader the "Start
    Reading" button opens."""
    if story.chapters.exists():
        return chapters_reading_minutes(story)
    if story.epub_file:
        minutes = epub_reading_minutes(story.epub_file)
        if minutes is not None:
            return minutes
    if story.pdf_file:
        return pdf_reading_minutes(story.pdf_file)
    return None


def probe_audio_duration_seconds(audio_file):
    try:
        import mutagen
    except ImportError:
        return None

    try:
        with audio_file.open("rb") as fileobj:
            info = mutagen.File(io.BytesIO(fileobj.read()))
    except Exception:
        return None

    if info is None or not info.info or not info.info.length:
        return None
    return float(info.info.length)


def story_listening_minutes(audios):
    durations = [audio.duration_seconds for audio in audios if audio.duration_seconds]
    if not durations:
        return None
    return max(1, round(sum(durations) / 60))

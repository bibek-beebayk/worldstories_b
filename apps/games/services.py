"""Pure domain services for the Name That Story daily puzzle game."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone as dt_timezone
import html
import random
import re

import nh3
from django.utils import timezone

from apps.stats.streaks import compute_streak
from apps.story.models import Story
from core.libs.images import get_cover_image_url

MAX_GUESSES = 6
CARD_COVER_SIZE = "480x640"

_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|h[1-6]|li|blockquote|hr|tr)\b[^>]*>", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'\u201c\u2018(\d])")
_ROMAN_NUMERAL_RE = re.compile(r"^[IVXLCDM]+\.?$", re.IGNORECASE)
_CHAPTER_HEADING_RE = re.compile(
    r"^(?:chapter|part|book|prologue|epilogue|section|act|scene)\b", re.IGNORECASE
)
_WHITESPACE_RE = re.compile(r"\s+")


def utc_today() -> date:
    return timezone.now().astimezone(dt_timezone.utc).date()


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def normalize_identity(text: str) -> str:
    return normalize_whitespace(text).lower()


def strip_html_to_text(raw_html: str | None, preserve_blocks: bool = False) -> str:
    source = str(raw_html or "")
    if preserve_blocks:
        source = _BLOCK_TAG_RE.sub("\n", source)
    cleaned = nh3.clean(source, tags=set(), attributes={})
    text = html.unescape(cleaned).replace("\u00a0", " ")
    if preserve_blocks:
        lines = [normalize_whitespace(line) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
    return normalize_whitespace(text)


def era_of(year: int | None) -> str | None:
    if year is None:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if y <= 0:
        return None
    return f"{(y // 10) * 10}s"


def length_label(story: Story) -> str:
    total_words = 0
    for content in story.chapters.values_list("content", flat=True):
        plain = strip_html_to_text(content)
        if plain:
            total_words += len(plain.split())
    if total_words < 12_000:
        return "Short story"
    if total_words <= 40_000:
        return "Novella"
    return "Novel"


@dataclass(frozen=True)
class BookFacts:
    """Shared normalized representation for both catalogue Story and external PuzzleBook."""

    key: str
    story_id: int | None
    book_id: int | None
    title: str
    author: str
    genre_ids: frozenset[int]
    genre_names: tuple[str, ...]
    country: str
    country_display: str
    year: int | None
    length: str
    cover_image_file: object | None
    cover_image: str | None
    slug: str | None
    views: int
    read_time: int | None
    in_catalogue: bool

    def cover_url(self, request=None) -> str:
        return (
            get_cover_image_url(
                self.cover_image_file,
                self.cover_image,
                request,
                size=CARD_COVER_SIZE,
            )
            or ""
        )


def facts_from_story(story: Story) -> BookFacts:
    genres = list(story.genres.order_by("name").values_list("id", "name"))
    genre_ids = frozenset(gid for gid, _ in genres)
    genre_names = tuple(name for _, name in genres)
    author_name = story.author.name if story.author_id else ""
    country_code = (story.country or "").strip().upper()
    country_display = story.get_country_display() if country_code else ""
    return BookFacts(
        key=f"story:{story.pk}",
        story_id=story.pk,
        book_id=None,
        title=story.title,
        author=author_name,
        genre_ids=genre_ids,
        genre_names=genre_names,
        country=country_code,
        country_display=country_display,
        year=story.original_published_year,
        length=length_label(story),
        cover_image_file=story.cover_image_file,
        cover_image=story.cover_image,
        slug=story.slug,
        views=int(story.views or 0),
        read_time=story.cached_chapter_reading_minutes,
        in_catalogue=True,
    )


def facts_from_book(book) -> BookFacts:
    genres = list(book.genres.order_by("name").values_list("id", "name"))
    genre_ids = frozenset(gid for gid, _ in genres)
    genre_names = tuple(name for _, name in genres)
    country_code = (book.country or "").strip().upper()
    country_display = book.get_country_display() if country_code else ""
    return BookFacts(
        key=f"book:{book.pk}",
        story_id=None,
        book_id=book.pk,
        title=book.title,
        author=book.author_name or "",
        genre_ids=genre_ids,
        genre_names=genre_names,
        country=country_code,
        country_display=country_display,
        year=book.original_published_year,
        length=book.length_label or "Novel",
        cover_image_file=book.cover_image_file,
        cover_image=book.cover_image,
        slug=None,
        views=0,
        read_time=None,
        in_catalogue=False,
    )


def to_book_facts(subject) -> BookFacts:
    if isinstance(subject, BookFacts):
        return subject
    if isinstance(subject, Story):
        return facts_from_story(subject)
    from apps.games.models import DailyPuzzle, PuzzleBook

    if isinstance(subject, PuzzleBook):
        return facts_from_book(subject)
    if isinstance(subject, DailyPuzzle):
        return subject.answer_facts
    raise TypeError(f"Cannot build BookFacts from {type(subject).__name__}")


def author_initials(subject) -> str | None:
    facts = to_book_facts(subject)
    name = (facts.author or "").strip()
    if not name:
        return None
    parts = [p for p in re.split(r"[\s]+", name) if p]
    initials = []
    for part in parts:
        letters = [ch for ch in part if ch.isalpha()]
        if letters:
            initials.append(f"{letters[0].upper()}.")
    return " ".join(initials) if initials else None


def same_work(a: BookFacts, b: BookFacts) -> bool:
    if a.key == b.key:
        return True
    title_a = normalize_identity(a.title)
    title_b = normalize_identity(b.title)
    if not title_a or title_a != title_b:
        return False
    return normalize_identity(a.author) == normalize_identity(b.author)


def compare_guess(answer_subject, guessed_subject) -> dict:
    ans = to_book_facts(answer_subject)
    gus = to_book_facts(guessed_subject)
    correct = same_work(ans, gus)

    genre_match = bool(ans.genre_ids and gus.genre_ids and (ans.genre_ids & gus.genre_ids))
    country_match = bool(ans.country and gus.country and ans.country == gus.country)

    if ans.year is None or gus.year is None:
        era_match = "different"
    elif era_of(ans.year) == era_of(gus.year):
        era_match = "same"
    elif abs(int(ans.year) - int(gus.year)) <= 10:
        era_match = "close"
    else:
        era_match = "different"

    return {
        "correct": correct,
        "matches": {
            "genre": genre_match,
            "country": country_match,
            "era": era_match,
        },
    }


def clues_for(puzzle_or_facts, unlocked_count: int, request=None) -> list[dict]:
    """Return ONLY the first `unlocked_count` clues (1..6) in strict reveal order."""
    count = max(0, min(int(unlocked_count), MAX_GUESSES))
    if count == 0:
        return []

    facts = to_book_facts(puzzle_or_facts)
    clues: list[dict] = []

    if count >= 1:
        clues.append(
            {
                "key": "genre",
                "label": "Genre",
                "value": ", ".join(facts.genre_names) if facts.genre_names else "Unknown",
            }
        )
    if count >= 2:
        clues.append(
            {
                "key": "country",
                "label": "Country",
                "value": facts.country_display or "Unknown",
            }
        )
    if count >= 3:
        clues.append(
            {
                "key": "era",
                "label": "Era",
                "value": era_of(facts.year) or "Unknown",
            }
        )
    if count >= 4:
        clues.append(
            {
                "key": "length",
                "label": "Length",
                "value": facts.length,
            }
        )
    if count >= 5:
        clues.append(
            {
                "key": "author_initials",
                "label": "Author initials",
                "value": author_initials(facts) or "Unknown",
            }
        )
    if count >= 6:
        clues.append(
            {
                "key": "cover",
                "label": "Blurred cover",
                "value": facts.cover_url(request),
            }
        )

    return clues


def _is_heading_like(line: str) -> bool:
    if not line:
        return True
    if _CHAPTER_HEADING_RE.match(line):
        return True
    if _ROMAN_NUMERAL_RE.match(line):
        return True
    letters = [ch for ch in line if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters) and len(line) <= 100:
        return True
    return False


def extract_candidates(story: Story, limit: int = 5) -> list[dict]:
    first_chapter = story.chapters.order_by("order").first()
    if first_chapter is None:
        return []

    block_text = strip_html_to_text(first_chapter.content, preserve_blocks=True)
    if not block_text:
        return []

    raw_sentences: list[str] = []
    for paragraph in block_text.splitlines():
        if _is_heading_like(paragraph):
            continue
        for sent in _SENTENCE_SPLIT_RE.split(paragraph):
            cleaned = normalize_whitespace(sent)
            if cleaned:
                raw_sentences.append(cleaned)

    filtered: list[dict] = []
    for idx, sent in enumerate(raw_sentences):
        if _is_heading_like(sent):
            continue
        is_first = len(filtered) == 0
        if len(sent) < 25 and not (is_first and len(sent) < 120):
            continue
        wc = len(sent.split())
        filtered.append({"index": idx, "text": sent, "word_count": wc})

    if not filtered:
        return []

    preferred = [c for c in filtered if 8 <= c["word_count"] <= 40]
    fallback = [c for c in filtered if c not in preferred]
    ordered = preferred + fallback
    return ordered[: max(1, int(limit))]


def opening_line_in_story(story: Story, opening_line: str) -> bool:
    needle = normalize_whitespace(opening_line)
    if not needle:
        return False
    needle_lower = needle.lower()
    for content in story.chapters.order_by("order").values_list("content", flat=True):
        haystack = strip_html_to_text(content).lower()
        if needle_lower in haystack:
            return True
    return False


def compute_difficulty(opening_line: str, subject) -> str:
    """Auto-compute easy/medium/hard using line word count and catalogue Story.views."""
    facts = to_book_facts(subject)
    word_count = len(normalize_whitespace(opening_line).split())
    # External books count as not popular (views = 0, in_catalogue = False).
    is_popular = facts.in_catalogue and facts.views >= 500
    if word_count < 14 or is_popular:
        return "easy"
    if word_count > 30 and not is_popular:
        return "hard"
    return "medium"


def find_matching_catalogue_story(title: str, author_name: str) -> Story | None:
    norm_title = normalize_identity(title)
    norm_author = normalize_identity(author_name)
    if not norm_title:
        return None
    for story in Story.objects.select_related("author").filter(title__iexact=normalize_whitespace(title)):
        s_title = normalize_identity(story.title)
        s_author = normalize_identity(story.author.name if story.author_id else "")
        if s_title == norm_title and s_author == norm_author:
            return story
    return None


def auto_fill(start_date: date, days: int) -> dict:
    from apps.games.models import DailyPuzzle

    created: list[dict] = []
    skipped: list[dict] = []

    published_stories = list(
        Story.objects.published()
        .filter(chapters__isnull=False)
        .distinct()
        .select_related("author")
        .order_by("id")
    )

    for offset in range(max(0, int(days))):
        target_date = start_date + timedelta(days=offset)
        if DailyPuzzle.objects.filter(date=target_date).exists():
            skipped.append({"date": target_date.isoformat(), "reason": "already_exists"})
            continue

        window_start = target_date - timedelta(days=180)
        window_end = target_date + timedelta(days=180)
        recently_used_ids = set(
            DailyPuzzle.objects.filter(
                date__gte=window_start,
                date__lte=window_end,
                story_id__isnull=False,
            ).values_list("story_id", flat=True)
        )

        pool = [s for s in published_stories if s.id not in recently_used_ids]
        rng = random.Random(f"name-that-story-{target_date.isoformat()}")
        rng.shuffle(pool)

        chosen = None
        chosen_line = None
        chosen_chapter = None
        for story in pool:
            candidates = extract_candidates(story, limit=3)
            if candidates:
                chosen = story
                chosen_line = candidates[0]["text"]
                chosen_chapter = story.chapters.order_by("order").first()
                break

        if chosen is None or not chosen_line:
            skipped.append({"date": target_date.isoformat(), "reason": "no_eligible_story"})
            continue

        puzzle = DailyPuzzle.objects.create(
            date=target_date,
            story=chosen,
            opening_line=chosen_line,
            source_chapter=chosen_chapter,
            auto_filled=True,
        )
        created.append(
            {
                "id": puzzle.id,
                "date": target_date.isoformat(),
                "story_id": chosen.id,
                "story_title": chosen.title,
            }
        )

    return {"created": created, "skipped": skipped}


def result_share_tokens(attempt) -> list[str]:
    """Return 6 slot tokens ('wrong' | 'right' | 'empty') — never the answer or opening line."""
    tokens: list[str] = []
    for entry in (attempt.guesses or [])[:MAX_GUESSES]:
        tokens.append("right" if entry.get("correct") else "wrong")
    while len(tokens) < MAX_GUESSES:
        tokens.append("empty")
    return tokens


def result_share_text(puzzle, attempt) -> dict:
    score = str(attempt.guess_count) if attempt.status == "solved" else "X"
    return {
        "header": f"Name That Story #{puzzle.number} \u00b7 {score}/{MAX_GUESSES}",
        "tokens": result_share_tokens(attempt),
    }


def stats_for(user=None, guest_key: str = "", today: date | None = None) -> dict:
    from apps.games.models import PuzzleAttempt

    today_utc = today or utc_today()
    qs = PuzzleAttempt.objects.select_related("puzzle").filter(
        status__in=[PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED]
    )
    if user is not None and getattr(user, "is_authenticated", False):
        qs = qs.filter(user=user)
    elif guest_key:
        qs = qs.filter(user__isnull=True, guest_key=guest_key)
    else:
        qs = qs.none()

    attempts = list(qs)
    played = len(attempts)
    wins = 0
    dist = {str(i): 0 for i in range(1, MAX_GUESSES + 1)}
    streak_dates: set[date] = set()

    for attempt in attempts:
        if attempt.status == PuzzleAttempt.STATUS_SOLVED:
            wins += 1
            if 1 <= attempt.guess_count <= MAX_GUESSES:
                dist[str(attempt.guess_count)] += 1
            if attempt.completed_at is not None:
                completed_utc_date = attempt.completed_at.astimezone(dt_timezone.utc).date()
                if completed_utc_date == attempt.puzzle.date:
                    streak_dates.add(attempt.puzzle.date)

    current_streak, best_streak = compute_streak(streak_dates, today_utc)
    win_pct = round((wins / played) * 100) if played > 0 else 0

    week_days = []
    for offset in range(6, -1, -1):
        d = today_utc - timedelta(days=offset)
        week_days.append(
            {
                "date": d.isoformat(),
                "label": d.strftime("%a")[0],
                "solved": d in streak_dates,
            }
        )

    return {
        "played": played,
        "wins": wins,
        "win_percentage": win_pct,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "guess_distribution": dist,
        "week_days": week_days,
    }

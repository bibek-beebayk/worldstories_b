"""Pure domain services for the Name That Story daily puzzle game."""

from __future__ import annotations

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


def author_initials(story: Story) -> str | None:
    author = getattr(story, "author", None)
    name = (getattr(author, "name", "") or "").strip() if author else ""
    if not name:
        return None
    parts = [p for p in re.split(r"[\s]+", name) if p]
    initials = []
    for part in parts:
        letters = [ch for ch in part if ch.isalpha()]
        if letters:
            initials.append(f"{letters[0].upper()}.")
    return " ".join(initials) if initials else None


def compare_guess(answer_story: Story, guessed_story: Story) -> dict:
    correct = answer_story.pk == guessed_story.pk

    answer_genres = set(answer_story.genres.values_list("id", flat=True))
    guessed_genres = set(guessed_story.genres.values_list("id", flat=True))
    genre_match = bool(answer_genres and guessed_genres and (answer_genres & guessed_genres))

    ans_country = (answer_story.country or "").strip().upper()
    guess_country = (guessed_story.country or "").strip().upper()
    country_match = bool(ans_country and guess_country and ans_country == guess_country)

    ans_year = answer_story.original_published_year
    guess_year = guessed_story.original_published_year
    if ans_year is None or guess_year is None:
        era_match = "different"
    elif era_of(ans_year) == era_of(guess_year):
        era_match = "same"
    elif abs(int(ans_year) - int(guess_year)) <= 10:
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


def clues_for(puzzle, unlocked_count: int, request=None) -> list[dict]:
    """Return ONLY the first `unlocked_count` clues (1..6) in strict reveal order."""
    count = max(0, min(int(unlocked_count), MAX_GUESSES))
    if count == 0:
        return []

    story = puzzle.story
    clues: list[dict] = []

    if count >= 1:
        genre_names = list(story.genres.order_by("name").values_list("name", flat=True))
        clues.append(
            {
                "key": "genre",
                "label": "Genre",
                "value": ", ".join(genre_names) if genre_names else "Unknown",
            }
        )
    if count >= 2:
        country_name = story.get_country_display() if story.country else ""
        clues.append(
            {
                "key": "country",
                "label": "Country",
                "value": country_name or "Unknown",
            }
        )
    if count >= 3:
        clues.append(
            {
                "key": "era",
                "label": "Era",
                "value": era_of(story.original_published_year) or "Unknown",
            }
        )
    if count >= 4:
        clues.append(
            {
                "key": "length",
                "label": "Length",
                "value": length_label(story),
            }
        )
    if count >= 5:
        clues.append(
            {
                "key": "author_initials",
                "label": "Author initials",
                "value": author_initials(story) or "Unknown",
            }
        )
    if count >= 6:
        cover_url = get_cover_image_url(
            story.cover_image_file,
            story.cover_image,
            request,
            size=CARD_COVER_SIZE,
        )
        clues.append(
            {
                "key": "cover",
                "label": "Blurred cover",
                "value": cover_url or "",
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

    # Prefer good opening lines (8..40 words), keeping the first qualifying sentence first.
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


def compute_difficulty(opening_line: str, story: Story) -> str:
    """Auto-compute easy/medium/hard using line word count and Story.views popularity."""
    word_count = len(normalize_whitespace(opening_line).split())
    # Popularity uses Story.views (top-read stories >= 500 views).
    is_popular = (getattr(story, "views", 0) or 0) >= 500
    if word_count < 14 or is_popular:
        return "easy"
    if word_count > 30 and not is_popular:
        return "hard"
    return "medium"


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
            DailyPuzzle.objects.filter(date__gte=window_start, date__lte=window_end).values_list(
                "story_id", flat=True
            )
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

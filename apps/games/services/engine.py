import random
from datetime import date, timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleRotation
from apps.stats.streaks import compute_streak
from apps.story.models import Author, Story

from .builders import get_builder
from .resolvers import get_resolver
from .text import word_count
from .validation import validate_for_approval

MAX_GUESSES = 6

DEFAULT_ROTATION: dict[int, str] = {
    0: DailyPuzzle.PUZZLE_TYPE_OPENING_LINE,  # Monday
    1: DailyPuzzle.PUZZLE_TYPE_TRIVIA,        # Tuesday
    2: DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO,    # Wednesday
    3: DailyPuzzle.PUZZLE_TYPE_QUOTE,         # Thursday
    4: DailyPuzzle.PUZZLE_TYPE_OPENING_LINE,  # Friday
    5: DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO,    # Saturday
    6: DailyPuzzle.PUZZLE_TYPE_TRIVIA,        # Sunday
}


def utc_today() -> date:
    return timezone.now().date()


def rotation_map() -> dict[int, str]:
    mapping = dict(DEFAULT_ROTATION)
    for row in PuzzleRotation.objects.all():
        mapping[row.weekday] = row.puzzle_type
    return mapping


def rotation_for_weekday(weekday: int) -> str:
    return rotation_map()[weekday % 7]


def answer_kind_for_type(puzzle_type: str) -> str:
    return (
        DailyPuzzle.ANSWER_KIND_AUTHOR
        if puzzle_type == DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO
        else DailyPuzzle.ANSWER_KIND_STORY
    )


def compute_difficulty(puzzle_like) -> str:
    if isinstance(puzzle_like, DailyPuzzle):
        clues = puzzle_like.clues or []
        answer_kind = puzzle_like.answer_kind
        answer_story = puzzle_like.answer_story
        answer_author = puzzle_like.answer_author
    else:
        clues = puzzle_like.get("clues") or []
        answer_kind = puzzle_like.get("answer_kind", "story")
        answer_story = puzzle_like.get("answer_story")
        answer_author = puzzle_like.get("answer_author")

    clue1_words = 0
    if clues and isinstance(clues[0], dict):
        if clues[0].get("kind") == "text":
            clue1_words = word_count(clues[0].get("value") or "")
        else:
            clue1_words = 14

    popularity = 0
    if answer_kind == "story" and answer_story is not None:
        popularity = int(getattr(answer_story, "views", 0) or 0)
    elif answer_kind == "author" and answer_author is not None:
        agg = (
            Story.objects.published()
            .filter(author=answer_author)
            .aggregate(total_views=Sum("views"))
        )
        popularity = int(agg["total_views"] or 0)

    # Combine clue-1 length and popularity (Story.views)
    score = 0
    if clue1_words >= 22:
        score += 1  # richer opening/quote clue is slightly easier
    elif clue1_words <= 10:
        score -= 1  # very short clue is harder

    if popularity >= 500:
        score += 1
    elif popularity < 50:
        score -= 1

    if score >= 1:
        return DailyPuzzle.DIFFICULTY_EASY
    if score <= -1:
        return DailyPuzzle.DIFFICULTY_HARD
    return DailyPuzzle.DIFFICULTY_MEDIUM


def unlocked_count_for(puzzle: DailyPuzzle, guesses_used: int) -> int:
    total_clues = len(puzzle.clues or [])
    if total_clues == 0:
        return 0
    return min(max(guesses_used, 0) + 1, total_clues)


def clues_for(puzzle: DailyPuzzle, unlocked_count: int, request=None) -> list[dict]:
    clues = puzzle.clues or []
    resolver = get_resolver(puzzle.answer_kind)
    answer = puzzle.answer
    attrs = resolver.attributes(answer, request=request) if answer is not None else {}

    result: list[dict] = []
    for idx, clue in enumerate(clues):
        position = idx + 1
        if idx >= unlocked_count:
            # CRITICAL: locked clues NEVER leak kind, label, key, or value
            result.append({"position": position, "locked": True})
            continue

        kind = clue.get("kind", "text")
        label = clue.get("label", f"Clue {position}")
        if kind == "text":
            value = clue.get("value", "")
        elif kind == "attribute":
            key = clue.get("key", "")
            value = attrs.get(key) or ""
        elif kind == "cover":
            value = attrs.get("cover") or ""
        else:
            value = ""

        result.append(
            {
                "position": position,
                "kind": kind,
                "label": label,
                "value": value,
                "locked": False,
            }
        )
    return result


def result_share_tokens(puzzle: DailyPuzzle, attempt: PuzzleAttempt) -> list[str]:
    tokens: list[str] = []
    for entry in (attempt.guesses or [])[:MAX_GUESSES]:
        if entry.get("correct"):
            tokens.append("right")
        else:
            tokens.append("wrong")
    while len(tokens) < MAX_GUESSES:
        tokens.append("empty")
    return tokens


def stats_for(user=None, guest_key: str = "") -> dict:
    if user is not None and getattr(user, "is_authenticated", False):
        qs = PuzzleAttempt.objects.filter(user=user)
    elif guest_key:
        qs = PuzzleAttempt.objects.filter(guest_key=guest_key)
    else:
        return {
            "played": 0,
            "wins": 0,
            "win_percentage": 0,
            "current_streak": 0,
            "best_streak": 0,
            "guess_distribution": {str(i): 0 for i in range(1, MAX_GUESSES + 1)},
            "week_history": [],
        }

    completed = list(
        qs.select_related("puzzle").filter(
            status__in=[PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED]
        )
    )
    played = len(completed)
    wins = sum(1 for a in completed if a.status == PuzzleAttempt.STATUS_SOLVED)
    win_pct = round((wins / played) * 100) if played else 0

    distribution = {str(i): 0 for i in range(1, MAX_GUESSES + 1)}
    non_archive_solved_dates: set[date] = set()
    status_by_date: dict[date, str] = {}

    for att in completed:
        status_by_date[att.puzzle.date] = att.status
        if att.status == PuzzleAttempt.STATUS_SOLVED:
            gc = min(max(att.guess_count, 1), MAX_GUESSES)
            distribution[str(gc)] += 1
            if att.completed_at and att.completed_at.date() == att.puzzle.date:
                non_archive_solved_dates.add(att.puzzle.date)

    today = utc_today()
    current_streak, best_streak = compute_streak(non_archive_solved_dates, today)

    week_history = []
    for offset in range(6, -1, -1):
        d = today - timedelta(days=offset)
        week_history.append(
            {
                "date": d.isoformat(),
                "status": status_by_date.get(d, "unplayed"),
            }
        )

    return {
        "played": played,
        "wins": wins,
        "win_percentage": win_pct,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "guess_distribution": distribution,
        "week_history": week_history,
    }


def auto_fill(start_date: date, days: int = 14) -> dict:
    days = max(1, min(int(days), 90))
    rot = rotation_map()

    existing_dates = set(
        DailyPuzzle.objects.filter(
            date__gte=start_date, date__lt=start_date + timedelta(days=days)
        ).values_list("date", flat=True)
    )

    # Pre-fetch recently used story & author IDs within +/- 180 days
    window_start = start_date - timedelta(days=180)
    window_end = start_date + timedelta(days=days + 180)
    used_story_ids: set[int] = set(
        DailyPuzzle.objects.filter(
            date__gte=window_start,
            date__lte=window_end,
            answer_story__isnull=False,
        ).values_list("answer_story_id", flat=True)
    )
    used_author_ids: set[int] = set(
        DailyPuzzle.objects.filter(
            date__gte=window_start,
            date__lte=window_end,
            answer_author__isnull=False,
        ).values_list("answer_author_id", flat=True)
    )

    candidate_stories = list(
        Story.objects.published()
        .select_related("author")
        .prefetch_related("genres", "chapters")
        .annotate(ch_count=Count("chapters"))
        .filter(ch_count__gt=0)
        .order_by("id")
    )
    candidate_authors = list(
        Author.objects.annotate(
            pub_count=Count("stories", filter=Q(stories__is_published=True))
        )
        .filter(pub_count__gte=2)
        .order_by("id")
    )

    created: list[str] = []
    drafted: list[str] = []
    skipped_empty: list[str] = []

    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        if target_date in existing_dates:
            continue

        puzzle_type = rot[target_date.weekday()]
        builder = get_builder(puzzle_type)
        if not builder.can_auto_build:
            skipped_empty.append(target_date.isoformat())
            continue

        rng = random.Random(int(target_date.strftime("%Y%m%d")))

        if puzzle_type == DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO:
            pool = [a for a in candidate_authors if a.id not in used_author_ids]
            rng.shuffle(pool)
            chosen_author = None
            chosen_clues = None
            chosen_valid = False
            fallback_author = None
            fallback_clues = None

            for author in pool:
                clues = builder.default_clues(author)
                errors = validate_for_approval(
                    {
                        "puzzle_type": puzzle_type,
                        "answer_kind": DailyPuzzle.ANSWER_KIND_AUTHOR,
                        "answer": author,
                        "clues": clues,
                    }
                )
                if not errors:
                    chosen_author = author
                    chosen_clues = clues
                    chosen_valid = True
                    break
                if fallback_author is None:
                    fallback_author = author
                    fallback_clues = clues

            if chosen_author is None and fallback_author is not None:
                chosen_author = fallback_author
                chosen_clues = fallback_clues
                chosen_valid = False

            if chosen_author is None:
                skipped_empty.append(target_date.isoformat())
                continue

            status = (
                DailyPuzzle.STATUS_APPROVED
                if chosen_valid
                else DailyPuzzle.STATUS_DRAFT
            )
            DailyPuzzle.objects.create(
                date=target_date,
                puzzle_type=puzzle_type,
                answer_kind=DailyPuzzle.ANSWER_KIND_AUTHOR,
                answer_author=chosen_author,
                clues=chosen_clues,
                status=status,
                auto_filled=True,
                approved_at=timezone.now() if chosen_valid else None,
            )
            used_author_ids.add(chosen_author.id)
            if chosen_valid:
                created.append(target_date.isoformat())
            else:
                drafted.append(target_date.isoformat())
        else:
            pool = [s for s in candidate_stories if s.id not in used_story_ids]
            rng.shuffle(pool)
            chosen_story = None
            chosen_clues = None
            chosen_chapter_id = None
            chosen_valid = False
            fallback_story = None
            fallback_clues = None
            fallback_chapter_id = None

            for story in pool:
                cands = builder.candidates(story, limit=3)
                if not cands:
                    continue
                first_cand = cands[0]
                clues = builder.default_clues(story, chosen_text=first_cand["text"])
                errors = validate_for_approval(
                    {
                        "puzzle_type": puzzle_type,
                        "answer_kind": DailyPuzzle.ANSWER_KIND_STORY,
                        "answer": story,
                        "clues": clues,
                    }
                )
                if not errors:
                    chosen_story = story
                    chosen_clues = clues
                    chosen_chapter_id = first_cand.get("chapter_id")
                    chosen_valid = True
                    break
                if fallback_story is None:
                    fallback_story = story
                    fallback_clues = clues
                    fallback_chapter_id = first_cand.get("chapter_id")

            if chosen_story is None and fallback_story is not None:
                chosen_story = fallback_story
                chosen_clues = fallback_clues
                chosen_chapter_id = fallback_chapter_id
                chosen_valid = False

            if chosen_story is None:
                skipped_empty.append(target_date.isoformat())
                continue

            status = (
                DailyPuzzle.STATUS_APPROVED
                if chosen_valid
                else DailyPuzzle.STATUS_DRAFT
            )
            DailyPuzzle.objects.create(
                date=target_date,
                puzzle_type=puzzle_type,
                answer_kind=DailyPuzzle.ANSWER_KIND_STORY,
                answer_story=chosen_story,
                source_chapter_id=chosen_chapter_id,
                clues=chosen_clues,
                status=status,
                auto_filled=True,
                approved_at=timezone.now() if chosen_valid else None,
            )
            used_story_ids.add(chosen_story.id)
            if chosen_valid:
                created.append(target_date.isoformat())
            else:
                drafted.append(target_date.isoformat())

    return {
        "created": created,
        "drafted": drafted,
        "skipped_empty": skipped_empty,
        "created_count": len(created),
        "drafted_count": len(drafted),
        "skipped_empty_count": len(skipped_empty),
    }

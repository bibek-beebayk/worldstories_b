import re

from apps.games.models import ClueBank, MiniCrossword

from .grid import GRID_SIZE, extract_entry_answer, number_grid
from .words import is_name, is_rare, zipf

DIFFICULTY_BASE_ZIPF = 5.0
DIFFICULTY_RARE_WEIGHT = 0.25
DIFFICULTY_NAME_WEIGHT = 0.4
DIFFICULTY_EASY_MAX_SCORE = 1.0
DIFFICULTY_HARD_MIN_SCORE = 1.45

_ALPHA_RE = re.compile(r"^[A-Z]+$")


def compute_difficulty(
    solution: list[str],
    pattern: list[str],
    include_db: bool = True,
) -> dict:
    entries = number_grid(pattern)["entries"]
    if not entries or not isinstance(solution, list) or len(solution) != GRID_SIZE:
        return {
            "label": MiniCrossword.DIFFICULTY_MEDIUM,
            "score": 0.0,
            "avg_zipf": 0.0,
            "rare_words": [],
            "names": [],
            "entry_count": 0,
        }

    answers = [extract_entry_answer(solution, e) for e in entries]
    if any(not _ALPHA_RE.match(a) for a in answers):
        return {
            "label": MiniCrossword.DIFFICULTY_MEDIUM,
            "score": 0.0,
            "avg_zipf": 0.0,
            "rare_words": [],
            "names": [],
            "entry_count": len(answers),
        }

    flagged_names: set[str] = set()
    if include_db:
        flagged_names = set(
            ClueBank.objects.filter(answer__in=answers, is_name=True).values_list(
                "answer", flat=True
            )
        )

    names = [a for a in answers if is_name(a, clue_bank_flag=(a in flagged_names))]
    name_set = set(names)
    rare_words = [a for a in answers if is_rare(a, clue_bank_flag=(a in flagged_names))]
    zipf_vals = [zipf(a, is_name_word=(a in name_set)) for a in answers]
    avg_zipf = round(sum(zipf_vals) / len(zipf_vals), 2)

    raw_score = (
        (DIFFICULTY_BASE_ZIPF - avg_zipf)
        + DIFFICULTY_RARE_WEIGHT * len(rare_words)
        + DIFFICULTY_NAME_WEIGHT * max(0, len(names) - 1)
    )
    score = round(max(0.0, raw_score), 2)

    if score < DIFFICULTY_EASY_MAX_SCORE:
        label = MiniCrossword.DIFFICULTY_EASY
    elif score >= DIFFICULTY_HARD_MIN_SCORE:
        label = MiniCrossword.DIFFICULTY_HARD
    else:
        label = MiniCrossword.DIFFICULTY_MEDIUM

    return {
        "label": label,
        "score": score,
        "avg_zipf": avg_zipf,
        "rare_words": rare_words,
        "names": names,
        "entry_count": len(answers),
    }


def apply_difficulty_to_puzzle(puzzle: MiniCrossword, include_db: bool = True) -> dict | None:
    if not puzzle.pattern or not puzzle.solution or len(puzzle.solution) != GRID_SIZE:
        return None
    breakdown = compute_difficulty(puzzle.solution, puzzle.pattern, include_db=include_db)
    if breakdown["entry_count"] == 0:
        return None
    puzzle.difficulty_score = breakdown["score"]
    if not puzzle.difficulty_override:
        puzzle.difficulty = breakdown["label"]
    return breakdown

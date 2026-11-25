import re

from apps.games.models import ClueBank, MiniCrossword

from .grid import GRID_SIZE, extract_entry_answer, number_grid
from .words import is_name, is_rare, zipf

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

    zipf_vals = [zipf(a) for a in answers]
    avg_zipf = round(sum(zipf_vals) / len(zipf_vals), 2)
    rare_words = [a for a in answers if is_rare(a)]
    names = [a for a in answers if is_name(a, clue_bank_flag=(a in flagged_names))]

    raw_score = (5.5 - avg_zipf) + 0.6 * len(rare_words) + 0.6 * len(names)
    score = round(max(0.0, raw_score), 2)

    if score < 1.0 and len(rare_words) == 0 and len(names) <= 1:
        label = MiniCrossword.DIFFICULTY_EASY
    elif score >= 2.2 or len(rare_words) >= 3 or len(names) >= 3:
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

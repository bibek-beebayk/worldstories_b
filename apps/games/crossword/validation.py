from datetime import timedelta
import re

from apps.games.models import MiniCrossword
from apps.games.services.engine import utc_today

from .grid import GRID_SIZE, extract_entry_answer, number_grid, validate_pattern
from .words import get_allowed_words, get_blocklist

_ALPHA_RE = re.compile(r"^[A-Z]$")
_CLEAN_RE = re.compile(r"[^A-Z0-9\s]+")


def _norm_text(value: str) -> str:
    return " ".join(_CLEAN_RE.sub(" ", (value or "").upper()).split())


def _clue_contains_answer(clue_text: str, answer: str) -> bool:
    norm_clue = _norm_text(clue_text)
    norm_ans = _norm_text(answer)
    if not norm_clue or not norm_ans:
        return False
    # Check whole word or exact token containment
    if f" {norm_ans} " in f" {norm_clue} ":
        return True
    return False


def validate_solution(
    pattern: list[str],
    solution: list[str],
    clues: list[dict],
    exclude_puzzle_id: int | None = None,
    include_db: bool = True,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    pattern_errors = validate_pattern(pattern)
    if pattern_errors:
        return {"errors": pattern_errors, "warnings": []}

    if not isinstance(solution, list) or len(solution) != GRID_SIZE:
        return {"errors": [f"Solution must be a list of {GRID_SIZE} rows."], "warnings": []}

    for r in range(GRID_SIZE):
        row = solution[r]
        if not isinstance(row, str) or len(row) != GRID_SIZE:
            errors.append(f"Solution row {r + 1} must be a string of length {GRID_SIZE}.")
            continue
        for c in range(GRID_SIZE):
            p_ch = pattern[r][c]
            s_ch = row[c]
            if p_ch == "#" and s_ch != "#":
                errors.append(f"Solution cell ({r + 1},{c + 1}) must be '#' to match pattern.")
            elif p_ch == "." and not _ALPHA_RE.match(s_ch):
                errors.append(
                    f"Solution cell ({r + 1},{c + 1}) must be an uppercase letter A-Z."
                )

    if errors:
        return {"errors": errors, "warnings": warnings}

    numbered = number_grid(pattern)
    entries = numbered["entries"]
    allowed = get_allowed_words(include_db=include_db)
    blocklist = get_blocklist()

    entry_answers: dict[tuple[int, str], str] = {}
    seen_answers: dict[str, str] = {}

    for entry in entries:
        ans = extract_entry_answer(solution, entry)
        key = (entry["number"], entry["direction"])
        entry_answers[key] = ans
        label = f"{entry['number']}-{entry['direction'].title()}"

        if ans in blocklist:
            errors.append(f"{label} ('{ans}') is on the blocklist.")
        elif ans not in allowed:
            errors.append(f"{label} ('{ans}') is not in the allowed word list.")

        if ans in seen_answers:
            errors.append(
                f"Duplicate answer '{ans}' appears in both {seen_answers[ans]} and {label}."
            )
        else:
            seen_answers[ans] = label

    if not isinstance(clues, list):
        errors.append("Clues must be a list.")
        return {"errors": errors, "warnings": warnings}

    clue_map: dict[tuple[int, str], dict] = {}
    for item in clues:
        if not isinstance(item, dict):
            errors.append("Each clue must be an object.")
            continue
        key = (item.get("number"), item.get("direction"))
        if key not in entry_answers:
            errors.append(f"Clue references unknown entry {key[0]}-{key[1]}.")
            continue
        if key in clue_map:
            errors.append(f"Duplicate clue for entry {key[0]}-{key[1]}.")
            continue
        clue_map[key] = item

    for entry in entries:
        key = (entry["number"], entry["direction"])
        label = f"{entry['number']}-{entry['direction'].title()}"
        ans = entry_answers[key]
        clue_obj = clue_map.get(key)
        if clue_obj is None:
            errors.append(f"Missing clue for {label} ({ans}).")
            continue
        clue_text = (clue_obj.get("clue") or "").strip()
        if not clue_text:
            errors.append(f"Clue for {label} ({ans}) cannot be empty.")
        elif _clue_contains_answer(clue_text, ans):
            errors.append(f"Clue for {label} contains its own answer '{ans}'.")

    # Non-blocking warnings: recently reused clues (60 days) or answers (30 days)
    if include_db:
        today = utc_today()
        qs = MiniCrossword.objects.filter(date__gte=today - timedelta(days=60))
        if exclude_puzzle_id:
            qs = qs.exclude(pk=exclude_puzzle_id)

        recent_clues_60: set[str] = set()
        recent_answers_30: set[str] = set()
        cutoff_30 = today - timedelta(days=30)

        for p in qs.only("date", "clues"):
            for c_item in p.clues or []:
                if not isinstance(c_item, dict):
                    continue
                ct = _norm_text(c_item.get("clue") or "")
                if ct:
                    recent_clues_60.add(ct)
                if p.date >= cutoff_30:
                    ca = (c_item.get("answer") or "").strip().upper()
                    if ca:
                        recent_answers_30.add(ca)

        for entry in entries:
            key = (entry["number"], entry["direction"])
            label = f"{entry['number']}-{entry['direction'].title()}"
            ans = entry_answers[key]
            if ans in recent_answers_30:
                warnings.append(f"{label} answer '{ans}' was used in the last 30 days.")
            clue_obj = clue_map.get(key)
            if clue_obj:
                ct = _norm_text(clue_obj.get("clue") or "")
                if ct and ct in recent_clues_60:
                    warnings.append(f"{label} clue text was used in the last 60 days.")

    return {"errors": errors, "warnings": warnings}

from datetime import date, timedelta
import random
import time

from apps.games.models import ClueBank, MiniAttempt, MiniCrossword
from apps.games.services.engine import utc_today
from apps.stats.streaks import compute_streak

from .difficulty import compute_difficulty
from .grid import GRID_SIZE, entry_cells, extract_entry_answer, number_grid
from .solver import (
    DEFAULT_MAX_NAMES,
    DEFAULT_PER_PATTERN_TIMEOUT,
    canonical_solution_keys,
    generate_fill,
)
from .words import DEFAULT_MIN_NON_NAME_WORDS, compute_bank_health

MAX_ELAPSED_SECONDS = 24 * 60 * 60  # 24 hours cap
DEFAULT_TOTAL_AUTO_TIMEOUT = 60.0

WEEKDAY_TARGET_DIFFICULTY: dict[int, str] = {
    0: MiniCrossword.DIFFICULTY_EASY,    # Monday
    1: MiniCrossword.DIFFICULTY_EASY,    # Tuesday
    2: MiniCrossword.DIFFICULTY_MEDIUM,  # Wednesday
    3: MiniCrossword.DIFFICULTY_MEDIUM,  # Thursday
    4: MiniCrossword.DIFFICULTY_MEDIUM,  # Friday
    5: MiniCrossword.DIFFICULTY_MEDIUM,  # Saturday
    6: MiniCrossword.DIFFICULTY_MEDIUM,  # Sunday
}

DEFAULT_AUTO_PATTERNS: list[list[str]] = [
    ["....#", ".....", ".....", ".....", "#...."],  # blacks at (0,4), (4,0)
    ["#....", ".....", ".....", ".....", "....#"],  # blacks at (0,0), (4,4)
    ["#...#", ".....", ".....", ".....", "....."],  # blacks at (0,0), (0,4)
    [".....", ".....", ".....", ".....", "#...#"],  # blacks at (4,0), (4,4)
    ["...##", ".....", ".....", ".....", "##..."],  # 3/5 across, 4/5 down
    ["##...", ".....", ".....", ".....", "...##"],  # 3/5 across, 4/5 down
    ["#...#", ".....", ".....", ".....", "#...#"],  # 4 corners black (3/5)
    [".....", ".....", ".....", ".....", "....."],  # full 5x5
]


def empty_letters_for_pattern(pattern: list[str]) -> list[str]:
    return [str(row) for row in pattern]


def draft_clues(
    solution: list[str],
    pattern: list[str],
    rng: random.Random | None = None,
) -> list[dict]:
    entries = number_grid(pattern)["entries"]
    answers = {extract_entry_answer(solution, e) for e in entries}
    bank_by_answer: dict[str, list[ClueBank]] = {}
    for row in ClueBank.objects.filter(answer__in=answers).order_by("times_used", "id"):
        bank_by_answer.setdefault(row.answer, []).append(row)

    picker = rng if rng is not None else random
    result: list[dict] = []
    for entry in entries:
        ans = extract_entry_answer(solution, entry)
        candidates = bank_by_answer.get(ans) or []
        chosen = None
        if candidates:
            min_used = candidates[0].times_used
            tied = [c for c in candidates if c.times_used == min_used]
            chosen = picker.choice(tied)
        result.append(
            {
                "number": entry["number"],
                "direction": entry["direction"],
                "answer": ans,
                "clue": chosen.clue if chosen else "",
                "story_id": chosen.story_id if chosen else None,
            }
        )
    return result


def grid_state(attempt: MiniAttempt) -> list[str]:
    if attempt.letters and len(attempt.letters) == GRID_SIZE:
        return list(attempt.letters)
    return empty_letters_for_pattern(attempt.puzzle.pattern)


def check_cells(
    puzzle: MiniCrossword,
    attempt: MiniAttempt,
    scope: str,
    row: int = 0,
    col: int = 0,
    direction: str = "across",
) -> list[list[int]]:
    letters = grid_state(attempt)
    solution = puzzle.solution

    if scope == "grid":
        target_cells = [
            (r, c)
            for r in range(GRID_SIZE)
            for c in range(GRID_SIZE)
            if puzzle.pattern[r][c] == "."
        ]
    else:
        entries = number_grid(puzzle.pattern)["entries"]
        chosen_entry = None
        for e in entries:
            if e["direction"] == direction and (row, col) in entry_cells(e):
                chosen_entry = e
                break
        if chosen_entry is None:
            for e in entries:
                if (row, col) in entry_cells(e):
                    chosen_entry = e
                    break
        target_cells = entry_cells(chosen_entry) if chosen_entry else []

    wrong: list[list[int]] = []
    for r, c in target_cells:
        entered = letters[r][c]
        if entered not in (".", "#") and entered != solution[r][c]:
            wrong.append([r, c])
    return wrong


def reveal_cell(puzzle: MiniCrossword, attempt: MiniAttempt, row: int, col: int) -> str:
    if not (0 <= row < GRID_SIZE and 0 <= col < GRID_SIZE):
        raise ValueError("Cell out of bounds.")
    if puzzle.pattern[row][col] != ".":
        raise ValueError("Cannot reveal a black square.")

    correct_letter = puzzle.solution[row][col]
    letters = grid_state(attempt)
    row_chars = list(letters[row])
    row_chars[col] = correct_letter
    letters[row] = "".join(row_chars)

    revealed = list(attempt.revealed or [])
    if [row, col] not in revealed:
        revealed.append([row, col])
        attempt.hints_used += 1

    attempt.letters = letters
    attempt.revealed = revealed
    attempt.save(update_fields=["letters", "revealed", "hints_used", "last_saved_at"])
    return correct_letter


def is_solved(puzzle: MiniCrossword, letters: list[str]) -> bool:
    if not isinstance(letters, list) or len(letters) != GRID_SIZE:
        return False
    return list(letters) == list(puzzle.solution)


def share_tokens(puzzle: MiniCrossword, attempt: MiniAttempt) -> list[str]:
    revealed_set = {(pair[0], pair[1]) for pair in (attempt.revealed or []) if len(pair) == 2}
    rows: list[str] = []
    for r in range(GRID_SIZE):
        chars: list[str] = []
        for c in range(GRID_SIZE):
            if puzzle.pattern[r][c] == "#":
                chars.append("K")
            elif (r, c) in revealed_set:
                chars.append("Y")
            else:
                chars.append("G")
        rows.append("".join(chars))
    return rows


def stats_for(user=None, guest_key: str = "") -> dict:
    if user is not None and getattr(user, "is_authenticated", False):
        qs = MiniAttempt.objects.filter(user=user)
    elif guest_key:
        qs = MiniAttempt.objects.filter(guest_key=guest_key)
    else:
        return {
            "played": 0,
            "solved": 0,
            "solved_percentage": 0,
            "average_time": None,
            "best_time": None,
            "current_streak": 0,
            "best_streak": 0,
            "last_7_times": [],
            "week_history": [],
        }

    attempts = list(qs.select_related("puzzle").order_by("puzzle__date"))
    played = len(attempts)
    solved_attempts = [a for a in attempts if a.status == MiniAttempt.STATUS_SOLVED]
    solved = len(solved_attempts)
    solved_pct = round((solved / played) * 100) if played else 0

    times = [a.elapsed_seconds for a in solved_attempts if a.elapsed_seconds is not None]
    avg_time = round(sum(times) / len(times)) if times else None
    best_time = min(times) if times else None

    non_archive_solved_dates: set[date] = set()
    status_by_date: dict[date, str] = {}
    for att in attempts:
        status_by_date[att.puzzle.date] = att.status
        if att.status == MiniAttempt.STATUS_SOLVED and not att.is_archive:
            non_archive_solved_dates.add(att.puzzle.date)

    today = utc_today()
    current_streak, best_streak = compute_streak(non_archive_solved_dates, today)

    last_7_solved = sorted(solved_attempts, key=lambda a: a.puzzle.date)[-7:]
    last_7_times = [
        {
            "date": a.puzzle.date.isoformat(),
            "elapsed_seconds": a.elapsed_seconds or 0,
            "hints_used": a.hints_used,
        }
        for a in last_7_solved
    ]

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
        "solved": solved,
        "solved_percentage": solved_pct,
        "average_time": avg_time,
        "best_time": best_time,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "last_7_times": last_7_times,
        "week_history": week_history,
    }


def auto_generate(
    start_date: date,
    days: int = 14,
    count: int | None = None,
    seed: int | str | None = None,
    patterns: list[list[str]] | None = None,
    per_pattern_timeout: float = DEFAULT_PER_PATTERN_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_AUTO_TIMEOUT,
    max_names: int = DEFAULT_MAX_NAMES,
    min_non_name_words: int = DEFAULT_MIN_NON_NAME_WORDS,
) -> dict:
    t_start = time.monotonic()
    deadline = t_start + max(0.5, float(total_timeout))
    pattern_list = list(patterns or DEFAULT_AUTO_PATTERNS)
    rng = random.Random(seed)

    # Determine target empty dates
    if count is not None:
        target_count = max(1, min(int(count), 90))
        scan_horizon = max(target_count * 3, 90)
        existing_dates = set(
            MiniCrossword.objects.filter(
                date__gte=start_date, date__lt=start_date + timedelta(days=scan_horizon)
            ).values_list("date", flat=True)
        )
        target_dates: list[date] = []
        offset = 0
        while len(target_dates) < target_count and offset < scan_horizon:
            d = start_date + timedelta(days=offset)
            if d not in existing_dates:
                target_dates.append(d)
            offset += 1
        requested = target_count
    else:
        days = max(1, min(int(days), 90))
        existing_dates = set(
            MiniCrossword.objects.filter(
                date__gte=start_date, date__lt=start_date + timedelta(days=days)
            ).values_list("date", flat=True)
        )
        target_dates = [
            start_date + timedelta(days=offset)
            for offset in range(days)
            if (start_date + timedelta(days=offset)) not in existing_dates
        ]
        requested = len(target_dates)

    # Preload existing full grids (and their 180-degree rotations) to avoid duplicates
    exclude_solutions: set[tuple[str, ...]] = set()
    for sol in MiniCrossword.objects.values_list("solution", flat=True):
        if isinstance(sol, list) and len(sol) == GRID_SIZE:
            sig_norm, sig_rot = canonical_solution_keys([str(r) for r in sol])
            exclude_solutions.add(sig_norm)
            exclude_solutions.add(sig_rot)

    created_dates: list[str] = []
    skipped_dates: list[str] = []

    for idx, target_date in enumerate(target_dates):
        if time.monotonic() >= deadline:
            skipped_dates.append(target_date.isoformat())
            continue

        ordered_patterns = list(pattern_list)
        rng.shuffle(ordered_patterns)
        grid_seed = rng.randrange(0, 1 << 31)
        target_diff = WEEKDAY_TARGET_DIFFICULTY[target_date.weekday()]

        chosen_pattern = None
        chosen_solution = None
        for p_idx, pat in enumerate(ordered_patterns):
            remaining_total = deadline - time.monotonic()
            if remaining_total <= 0.05:
                break
            pat_budget = min(float(per_pattern_timeout), remaining_total)
            sols = generate_fill(
                pat,
                seed=f"{grid_seed}:{p_idx}",
                limit=1,
                timeout_seconds=pat_budget,
                target_difficulty=target_diff,
                max_names=max_names,
                min_non_name_words=min_non_name_words,
                exclude_solutions=exclude_solutions,
            )
            if sols:
                chosen_pattern = pat
                chosen_solution = sols[0]
                break

        if chosen_pattern is None or chosen_solution is None:
            skipped_dates.append(target_date.isoformat())
            continue

        sig_norm, sig_rot = canonical_solution_keys(chosen_solution)
        exclude_solutions.add(sig_norm)
        exclude_solutions.add(sig_rot)

        clues = draft_clues(chosen_solution, chosen_pattern, rng=rng)
        diff = compute_difficulty(chosen_solution, chosen_pattern)
        MiniCrossword.objects.create(
            date=target_date,
            title="",
            pattern=chosen_pattern,
            solution=chosen_solution,
            clues=clues,
            difficulty=diff["label"],
            difficulty_score=diff["score"],
            difficulty_override=False,
            status=MiniCrossword.STATUS_DRAFT,  # NEVER auto-approve
            auto_generated=True,
        )
        created_dates.append(target_date.isoformat())

    health = compute_bank_health()
    reason = "bank_too_small" if (len(created_dates) == 0 and requested > 0) else None
    suggestion = health["suggestion"] if reason == "bank_too_small" else ""

    return {
        "created": len(created_dates),
        "created_count": len(created_dates),
        "created_dates": created_dates,
        "requested": requested,
        "skipped": skipped_dates,
        "skipped_count": len(skipped_dates),
        "reason": reason,
        "bank": {
            "total_answers": health["total_answers"],
            "by_length": health["by_length"],
            "scarce_letters": health["scarce_letters"],
        },
        "suggestion": suggestion,
        "elapsed_seconds": round(time.monotonic() - t_start, 2),
    }

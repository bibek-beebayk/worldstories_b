from datetime import date, timedelta

from apps.games.models import ClueBank, MiniAttempt, MiniCrossword
from apps.games.services.engine import utc_today
from apps.stats.streaks import compute_streak

from .difficulty import compute_difficulty
from .grid import GRID_SIZE, entry_cells, extract_entry_answer, number_grid
from .solver import generate_fill

MAX_ELAPSED_SECONDS = 24 * 60 * 60  # 24 hours cap

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
]


def empty_letters_for_pattern(pattern: list[str]) -> list[str]:
    return [str(row) for row in pattern]


def draft_clues(solution: list[str], pattern: list[str]) -> list[dict]:
    entries = number_grid(pattern)["entries"]
    answers = {extract_entry_answer(solution, e) for e in entries}
    bank_by_answer: dict[str, list[ClueBank]] = {}
    for row in ClueBank.objects.filter(answer__in=answers).order_by("times_used", "id"):
        bank_by_answer.setdefault(row.answer, []).append(row)

    result: list[dict] = []
    for entry in entries:
        ans = extract_entry_answer(solution, entry)
        candidates = bank_by_answer.get(ans) or []
        chosen = candidates[0] if candidates else None
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
    patterns: list[list[str]] | None = None,
) -> dict:
    days = max(1, min(int(days), 90))
    pattern_list = patterns or DEFAULT_AUTO_PATTERNS

    existing_dates = set(
        MiniCrossword.objects.filter(
            date__gte=start_date, date__lt=start_date + timedelta(days=days)
        ).values_list("date", flat=True)
    )

    created: list[str] = []
    skipped: list[str] = []

    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        if target_date in existing_dates:
            continue

        start_idx = target_date.toordinal() % len(pattern_list)
        ordered_patterns = [
            pattern_list[(start_idx + i) % len(pattern_list)]
            for i in range(len(pattern_list))
        ]
        seed = int(target_date.strftime("%Y%m%d"))
        target_diff = WEEKDAY_TARGET_DIFFICULTY[target_date.weekday()]

        chosen_pattern = None
        chosen_solution = None
        for pat in ordered_patterns:
            sols = generate_fill(
                pat,
                seed=seed,
                limit=1,
                timeout_seconds=2.0,
                target_difficulty=target_diff,
            )
            if sols:
                chosen_pattern = pat
                chosen_solution = sols[0]
                break

        if chosen_pattern is None or chosen_solution is None:
            skipped.append(target_date.isoformat())
            continue

        clues = draft_clues(chosen_solution, chosen_pattern)
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
        created.append(target_date.isoformat())

    return {
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }

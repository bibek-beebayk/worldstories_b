import random
import time

from apps.games.models import ClueBank

from .grid import GRID_SIZE, extract_entry_answer, number_grid, validate_pattern
from .words import get_allowed_words


def _row_slots(pattern: list[str]) -> list[list[tuple[int, int]]]:
    """For each row r, return list of (start_col, length) white runs."""
    rows: list[list[tuple[int, int]]] = []
    for r in range(GRID_SIZE):
        slots = []
        c = 0
        while c < GRID_SIZE:
            if pattern[r][c] == ".":
                start = c
                while c < GRID_SIZE and pattern[r][c] == ".":
                    c += 1
                slots.append((start, c - start))
            else:
                c += 1
        rows.append(slots)
    return rows


def _col_segments(pattern: list[str]) -> list[list[tuple[int, int]]]:
    """For each col c, return list of (start_row, length) white runs."""
    cols: list[list[tuple[int, int]]] = []
    for c in range(GRID_SIZE):
        segs = []
        r = 0
        while r < GRID_SIZE:
            if pattern[r][c] == ".":
                start = r
                while r < GRID_SIZE and pattern[r][c] == ".":
                    r += 1
                segs.append((start, r - start))
            else:
                r += 1
        cols.append(segs)
    return cols


def generate_fill(
    pattern: list[str],
    seed: int | str | None = None,
    theme_words: list[str] | None = None,
    limit: int = 3,
    timeout_seconds: float = 2.0,
    include_db: bool = True,
) -> list[list[str]]:
    if validate_pattern(pattern):
        return []

    allowed = get_allowed_words(include_db=include_db)
    if not allowed:
        return []

    theme_set = {(w or "").strip().upper() for w in (theme_words or []) if w}
    bank_set: set[str] = set()
    if include_db:
        bank_set = set(ClueBank.objects.values_list("answer", flat=True))
    preferred = theme_set | bank_set

    rng = random.Random(seed)

    # Group words by length and build prefix sets
    words_by_len: dict[int, list[str]] = {3: [], 4: [], 5: []}
    prefixes_by_len: dict[int, set[str]] = {3: set(), 4: set(), 5: set()}
    for w in sorted(allowed):
        n = len(w)
        if n in words_by_len:
            words_by_len[n].append(w)
            for k in range(1, n + 1):
                prefixes_by_len[n].add(w[:k])

    # Deterministic order: preferred words first (shuffled), then remaining words (shuffled)
    for n in (3, 4, 5):
        pref = [w for w in words_by_len[n] if w in preferred]
        rest = [w for w in words_by_len[n] if w not in preferred]
        rng.shuffle(pref)
        rng.shuffle(rest)
        words_by_len[n] = pref + rest

    row_slots = _row_slots(pattern)
    col_segs = _col_segments(pattern)
    entries = number_grid(pattern)["entries"]

    # Map each (r, c) to its column segment (start_row, length)
    col_seg_for_cell: dict[tuple[int, int], tuple[int, int]] = {}
    for c in range(GRID_SIZE):
        for start_r, length in col_segs[c]:
            for rr in range(start_r, start_r + length):
                col_seg_for_cell[(rr, c)] = (start_r, length)

    # Pre-build index of words by (length, pos, char) for fast pruning? Even simpler:
    # For each row, we fill its 1 (always 1 in valid 5x5 patterns since min run is 3) across slot!
    # Verify: in a 5-col row with min run 3, a row can have at most ONE white run!
    grid = [list(row) for row in pattern]
    used_words: set[str] = set()
    solutions: list[tuple[int, list[str]]] = []
    seen_sigs: set[tuple[str, ...]] = set()
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    max_collect = max(limit * 4, 12)

    def _score_solution(sol: list[str]) -> int:
        return sum(1 for e in entries if extract_entry_answer(sol, e) in preferred)

    def _backtrack(row_idx: int):
        if len(solutions) >= max_collect or time.monotonic() >= deadline:
            return
        if row_idx == GRID_SIZE:
            # Verify all completed column words are distinct from across words & each other
            down_words = []
            for c in range(GRID_SIZE):
                for start_r, length in col_segs[c]:
                    w = "".join(grid[rr][c] for rr in range(start_r, start_r + length))
                    if w in used_words or w in down_words:
                        return
                    down_words.append(w)
            sol = ["".join(r) for r in grid]
            sig = tuple(sol)
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                solutions.append((_score_solution(sol), sol))
            return

        slots = row_slots[row_idx]
        if not slots:
            _backtrack(row_idx + 1)
            return

        start_c, length = slots[0]
        candidates = words_by_len[length]

        for word in candidates:
            if time.monotonic() >= deadline or len(solutions) >= max_collect:
                return
            if word in used_words:
                continue

            # Place word & check column prefixes immediately
            ok = True
            completed_cols: list[str] = []
            for i, ch in enumerate(word):
                c = start_c + i
                grid[row_idx][c] = ch
                seg_start, seg_len = col_seg_for_cell[(row_idx, c)]
                prefix = "".join(grid[rr][c] for rr in range(seg_start, row_idx + 1))
                if prefix not in prefixes_by_len[seg_len]:
                    ok = False
                    break
                if len(prefix) == seg_len:
                    if prefix == word or prefix in used_words or prefix in completed_cols:
                        ok = False
                        break
                    completed_cols.append(prefix)

            if ok:
                used_words.add(word)
                _backtrack(row_idx + 1)
                used_words.remove(word)

        # Restore row to dots
        for i in range(length):
            grid[row_idx][start_c + i] = "."

    _backtrack(0)

    solutions.sort(key=lambda pair: -pair[0])
    return [sol for _, sol in solutions[:limit]]

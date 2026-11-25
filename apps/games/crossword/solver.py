import random
import time

from .difficulty import compute_difficulty
from .grid import GRID_SIZE, extract_entry_answer, number_grid, validate_pattern
from .words import get_solver_candidate_words, is_name, zipf


def _row_slots(pattern: list[str]) -> list[list[tuple[int, int]]]:
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
    target_difficulty: str | None = None,
) -> list[list[str]]:
    if validate_pattern(pattern):
        return []

    pool, bank_non_names, flagged_names = get_solver_candidate_words(include_db=include_db)
    if not pool:
        return []

    theme_set = {(w or "").strip().upper() for w in (theme_words or []) if w}
    preferred = theme_set | bank_non_names

    rng = random.Random(seed)

    # Pre-classify name status for every word in pool
    name_words = {w for w in pool if is_name(w, clue_bank_flag=(w in flagged_names))}

    words_by_len: dict[int, list[str]] = {3: [], 4: [], 5: []}
    prefixes_by_len: dict[int, set[str]] = {3: set(), 4: set(), 5: set()}
    for w in sorted(pool):
        n = len(w)
        if n in words_by_len:
            words_by_len[n].append(w)
            for k in range(1, n + 1):
                prefixes_by_len[n].add(w[:k])

    # Order candidates: prefer high-Zipf / ClueBank / theme non-names first, shuffled deterministically
    for n in (3, 4, 5):
        tier_pref = [w for w in words_by_len[n] if w in preferred and w not in name_words]
        tier_common = [
            w
            for w in words_by_len[n]
            if w not in preferred and w not in name_words and zipf(w) >= 4.2
        ]
        tier_mid = [
            w
            for w in words_by_len[n]
            if w not in preferred and w not in name_words and zipf(w) < 4.2
        ]
        tier_names = [w for w in words_by_len[n] if w in name_words]
        rng.shuffle(tier_pref)
        rng.shuffle(tier_common)
        rng.shuffle(tier_mid)
        rng.shuffle(tier_names)
        words_by_len[n] = tier_pref + tier_common + tier_mid + tier_names

    row_slots = _row_slots(pattern)
    col_segs = _col_segments(pattern)
    entries = number_grid(pattern)["entries"]

    col_seg_for_cell: dict[tuple[int, int], tuple[int, int]] = {}
    for c in range(GRID_SIZE):
        for start_r, length in col_segs[c]:
            for rr in range(start_r, start_r + length):
                col_seg_for_cell[(rr, c)] = (start_r, length)

    grid = [list(row) for row in pattern]
    used_words: set[str] = set()
    solutions: list[tuple[int, float, list[str]]] = []
    seen_sigs: set[tuple[str, ...]] = set()
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    max_collect = max(limit * 2, 4)
    max_steps = 2000000
    steps_visited = 0

    def _score_solution(sol: list[str]) -> tuple[int, float]:
        answers = [extract_entry_answer(sol, e) for e in entries]
        z_sum = sum(zipf(a) for a in answers)
        bank_bonus = sum(1.5 for a in answers if a in preferred)
        name_penalty = sum(2.0 for a in answers if a in name_words)
        total_score = z_sum + bank_bonus - name_penalty
        target_match = 0
        if target_difficulty:
            diff = compute_difficulty(sol, pattern, include_db=include_db)
            if diff["label"] == target_difficulty:
                target_match = 1
        return target_match, total_score

    def _backtrack(row_idx: int, names_used: int):
        nonlocal steps_visited
        if (
            len(solutions) >= max_collect
            or steps_visited >= max_steps
            or time.monotonic() >= deadline
        ):
            return
        if row_idx == GRID_SIZE:
            down_words = []
            total_names = names_used
            for c in range(GRID_SIZE):
                for start_r, length in col_segs[c]:
                    w = "".join(grid[rr][c] for rr in range(start_r, start_r + length))
                    if w in used_words or w in down_words:
                        return
                    if w in name_words:
                        total_names += 1
                        if total_names > 1:
                            return
                    down_words.append(w)

            sol = ["".join(r) for r in grid]
            sig = tuple(sol)
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                t_match, score = _score_solution(sol)
                solutions.append((t_match, score, sol))
            return

        slots = row_slots[row_idx]
        if not slots:
            _backtrack(row_idx + 1, names_used)
            return

        start_c, length = slots[0]
        candidates = words_by_len[length]

        for word in candidates:
            steps_visited += 1
            if (
                steps_visited >= max_steps
                or (steps_visited & 1023 == 0 and time.monotonic() >= deadline)
                or len(solutions) >= max_collect
            ):
                return
            if word in used_words:
                continue
            word_is_name = word in name_words
            if word_is_name and names_used >= 1:
                continue

            ok = True
            completed_cols: list[str] = []
            completed_col_names = 0
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
                    if prefix in name_words:
                        completed_col_names += 1
                        if names_used + (1 if word_is_name else 0) + completed_col_names > 1:
                            ok = False
                            break
                    completed_cols.append(prefix)

            if ok:
                used_words.add(word)
                _backtrack(row_idx + 1, names_used + (1 if word_is_name else 0))
                used_words.remove(word)

        for i in range(length):
            grid[row_idx][start_c + i] = "."

    _backtrack(0, 0)

    solutions.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [sol for _, _, sol in solutions[:limit]]

import random
import time

from .difficulty import compute_difficulty
from .grid import GRID_SIZE, entry_cells, extract_entry_answer, number_grid, validate_pattern
from .words import DEFAULT_MIN_NON_NAME_WORDS, get_bank_word_index, zipf

DEFAULT_MAX_NAMES = 1
DEFAULT_PER_PATTERN_TIMEOUT = 5.0


def canonical_solution_keys(solution: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (normal, rotated_180) signatures for full-grid deduplication."""
    normal = tuple(solution)
    rotated = tuple(row[::-1] for row in reversed(solution))
    return normal, rotated


def generate_fill(
    pattern: list[str],
    seed: int | str | None = None,
    theme_words: list[str] | None = None,
    limit: int = 3,
    timeout_seconds: float = DEFAULT_PER_PATTERN_TIMEOUT,
    include_db: bool = True,
    target_difficulty: str | None = None,
    max_names: int = DEFAULT_MAX_NAMES,
    min_non_name_words: int = DEFAULT_MIN_NON_NAME_WORDS,
    exclude_solutions: set[tuple[str, ...]] | None = None,
) -> list[list[str]]:
    """Fill a 5x5 pattern using ONLY ClueBank words via MRV + bitmask constraint propagation."""
    if validate_pattern(pattern) or not include_db:
        return []

    bank = get_bank_word_index()
    if not bank.answers:
        return []

    # Skip names unless fewer than min_non_name_words non-name words remain
    allow_names = len(bank.non_name_words) < min_non_name_words and max_names > 0
    active_pool = bank.answers if allow_names else bank.non_name_words
    if not active_pool:
        return []

    name_words = bank.flagged_names if allow_names else frozenset()
    theme_set = {(w or "").strip().upper() for w in (theme_words or []) if w}
    excluded_sigs = exclude_solutions or set()

    rng = random.Random(seed)

    entries = number_grid(pattern)["entries"]
    num_slots = len(entries)
    if num_slots == 0:
        return []

    slot_cells = [entry_cells(e) for e in entries]
    slot_lengths = [e["length"] for e in entries]

    # Build crossing map: for each slot s, list of (pos_in_s, other_slot, pos_in_other)
    cell_to_slots: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for s_idx, cells in enumerate(slot_cells):
        for pos, rc in enumerate(cells):
            cell_to_slots.setdefault(rc, []).append((s_idx, pos))

    crossings: list[list[tuple[int, int, int]]] = [[] for _ in range(num_slots)]
    for rc, occurrences in cell_to_slots.items():
        if len(occurrences) == 2:
            (s1, p1), (s2, p2) = occurrences
            crossings[s1].append((p1, s2, p2))
            crossings[s2].append((p2, s1, p1))

    # Build ordered word lists & (pos, letter) bitmasks per required length
    needed_lengths = set(slot_lengths)
    words_by_len: dict[int, list[str]] = {}
    is_name_by_len: dict[int, list[bool]] = {}
    pos_masks: dict[int, dict[tuple[int, str], int]] = {}
    full_mask_by_len: dict[int, int] = {}

    for length in needed_lengths:
        candidates = [w for w in bank.answers_by_len.get(length, ()) if w in active_pool]
        if not candidates:
            return []

        # Order: theme words first, then grouped by min_times_used (least used first),
        # shuffled within each usage bucket for seed-driven variety.
        by_bucket: dict[tuple[int, int], list[str]] = {}
        for w in candidates:
            is_theme = 0 if w in theme_set else 1
            used = bank.min_times_used.get(w, 0)
            by_bucket.setdefault((is_theme, used), []).append(w)

        ordered: list[str] = []
        for bucket_key in sorted(by_bucket.keys()):
            bucket = by_bucket[bucket_key]
            rng.shuffle(bucket)
            ordered.extend(bucket)

        words_by_len[length] = ordered
        is_name_by_len[length] = [w in name_words for w in ordered]
        full_mask_by_len[length] = (1 << len(ordered)) - 1

        pm: dict[tuple[int, str], int] = {}
        for idx, word in enumerate(ordered):
            bit = 1 << idx
            for pos, ch in enumerate(word):
                key = (pos, ch)
                pm[key] = pm.get(key, 0) | bit
        pos_masks[length] = pm

    # Initial domains per slot
    init_domains = [full_mask_by_len[length] for length in slot_lengths]

    # Arc-consistency pass (AC-3 style) before backtracking to prune dead words fast
    def _ac3(domains: list[int]) -> bool:
        queue = list(range(num_slots))
        in_queue = set(queue)
        while queue:
            s = queue.pop()
            in_queue.discard(s)
            dom_s = domains[s]
            if dom_s == 0:
                return False
            len_s = slot_lengths[s]
            words_s = words_by_len[len_s]
            for p_s, other, p_o in crossings[s]:
                # Collect letters available at p_s in dom_s
                avail_chars: set[str] = set()
                m = dom_s
                while m:
                    lsb = m & -m
                    idx = lsb.bit_length() - 1
                    avail_chars.add(words_s[idx][p_s])
                    m ^= lsb
                    if len(avail_chars) == 26:
                        break
                pm_o = pos_masks[slot_lengths[other]]
                allowed_o = 0
                for ch in avail_chars:
                    allowed_o |= pm_o.get((p_o, ch), 0)
                new_dom_o = domains[other] & allowed_o
                if new_dom_o == 0:
                    return False
                if new_dom_o != domains[other]:
                    domains[other] = new_dom_o
                    if other not in in_queue:
                        queue.append(other)
                        in_queue.add(other)
        return True

    if not _ac3(init_domains):
        return []

    assigned: list[str | None] = [None] * num_slots
    used_words: set[str] = set()
    solutions: list[tuple[int, float, float, list[str]]] = []
    seen_sigs: set[tuple[str, ...]] = set(excluded_sigs)
    deadline = time.monotonic() + max(0.05, float(timeout_seconds))
    max_collect = max(limit * 2, 2)
    steps = 0

    def _build_solution() -> list[str]:
        grid = [list(row) for row in pattern]
        for s_idx, word in enumerate(assigned):
            assert word is not None
            for pos, (r, c) in enumerate(slot_cells[s_idx]):
                grid[r][c] = word[pos]
        return ["".join(row) for row in grid]

    def _score_solution(sol: list[str]) -> tuple[int, float, float]:
        answers = [extract_entry_answer(sol, e) for e in entries]
        total_used = sum(bank.min_times_used.get(a, 0) for a in answers)
        z_sum = sum(zipf(a) for a in answers)
        t_match = 0
        if target_difficulty:
            diff = compute_difficulty(sol, pattern, include_db=True)
            if diff["label"] == target_difficulty:
                t_match = 1
        # Higher is better: prefer target difficulty match, lower usage, higher Zipf
        return t_match, -float(total_used), z_sum

    def _backtrack(domains: list[int], remaining: int, names_used: int) -> None:
        nonlocal steps
        if len(solutions) >= max_collect:
            return
        if (steps & 255) == 0 and time.monotonic() >= deadline:
            return

        if remaining == 0:
            # Never auto-generate a grid whose answers are all names
            if names_used >= num_slots:
                return
            sol = _build_solution()
            sig_norm, sig_rot = canonical_solution_keys(sol)
            if sig_norm in seen_sigs or sig_rot in seen_sigs:
                return
            seen_sigs.add(sig_norm)
            seen_sigs.add(sig_rot)
            t_match, neg_used, z_sum = _score_solution(sol)
            solutions.append((t_match, neg_used, z_sum, sol))
            return

        # Most-constrained-first (MRV): pick unassigned slot with smallest domain
        best_slot = -1
        best_count = 1 << 30
        for s in range(num_slots):
            if assigned[s] is None:
                cnt = domains[s].bit_count()
                if cnt == 0:
                    return
                if cnt < best_count:
                    best_count = cnt
                    best_slot = s
                    if cnt == 1:
                        break

        length = slot_lengths[best_slot]
        words = words_by_len[length]
        flags = is_name_by_len[length]
        slot_crossings = crossings[best_slot]
        dom = domains[best_slot]

        m = dom
        while m:
            if len(solutions) >= max_collect:
                return
            steps += 1
            if (steps & 255) == 0 and time.monotonic() >= deadline:
                return

            lsb = m & -m
            idx = lsb.bit_length() - 1
            m ^= lsb

            word = words[idx]
            if word in used_words:
                continue

            word_is_name = flags[idx]
            next_names = names_used + (1 if word_is_name else 0)
            if next_names > max_names or next_names >= num_slots:
                continue

            # Forward-check all unassigned crossings
            next_domains = list(domains)
            next_domains[best_slot] = lsb
            ok = True
            for p_s, other, p_o in slot_crossings:
                if assigned[other] is None:
                    ch = word[p_s]
                    nd = next_domains[other] & pos_masks[slot_lengths[other]].get((p_o, ch), 0)
                    if nd == 0:
                        ok = False
                        break
                    next_domains[other] = nd

            if not ok:
                continue

            assigned[best_slot] = word
            used_words.add(word)
            _backtrack(next_domains, remaining - 1, next_names)
            used_words.remove(word)
            assigned[best_slot] = None

    _backtrack(init_domains, num_slots, 0)

    solutions.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [sol for _, _, _, sol in solutions[:limit]]

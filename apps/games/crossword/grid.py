GRID_SIZE = 5
MIN_WHITE_CELLS = 15
MIN_WORD_LENGTH = 3


def validate_pattern(pattern) -> list[str]:
    errors: list[str] = []
    if not isinstance(pattern, list) or len(pattern) != GRID_SIZE:
        return [f"Pattern must be a list of {GRID_SIZE} rows."]

    white_count = 0
    for r, row in enumerate(pattern):
        if not isinstance(row, str) or len(row) != GRID_SIZE:
            errors.append(f"Row {r + 1} must be a string of length {GRID_SIZE}.")
            continue
        for c, ch in enumerate(row):
            if ch not in ("#", "."):
                errors.append(f"Invalid character '{ch}' at row {r + 1}, col {c + 1}.")
            elif ch == ".":
                white_count += 1

    if errors:
        return errors

    if white_count < MIN_WHITE_CELLS:
        errors.append(
            f"Pattern has {white_count} white cells; at least {MIN_WHITE_CELLS} are required."
        )

    for r in range(GRID_SIZE):
        for c in range(GRID_SIZE):
            if pattern[r][c] != ".":
                continue

            # Across run length containing (r, c)
            c0 = c
            while c0 > 0 and pattern[r][c0 - 1] == ".":
                c0 -= 1
            c1 = c
            while c1 < GRID_SIZE - 1 and pattern[r][c1 + 1] == ".":
                c1 += 1
            across_len = c1 - c0 + 1

            # Down run length containing (r, c)
            r0 = r
            while r0 > 0 and pattern[r0 - 1][c] == ".":
                r0 -= 1
            r1 = r
            while r1 < GRID_SIZE - 1 and pattern[r1 + 1][c] == ".":
                r1 += 1
            down_len = r1 - r0 + 1

            if across_len < MIN_WORD_LENGTH:
                errors.append(
                    f"Cell ({r + 1},{c + 1}) belongs to an across run of length {across_len} (minimum {MIN_WORD_LENGTH})."
                )
            if down_len < MIN_WORD_LENGTH:
                errors.append(
                    f"Cell ({r + 1},{c + 1}) belongs to a down run of length {down_len} (minimum {MIN_WORD_LENGTH})."
                )

    return errors


def number_grid(pattern: list[str]) -> dict:
    numbering: list[list[int | None]] = [
        [None for _ in range(GRID_SIZE)] for _ in range(GRID_SIZE)
    ]
    entries: list[dict] = []
    current_num = 1

    for r in range(GRID_SIZE):
        for c in range(GRID_SIZE):
            if pattern[r][c] != ".":
                continue

            starts_across = (c == 0 or pattern[r][c - 1] == "#") and (
                c + 1 < GRID_SIZE and pattern[r][c + 1] == "."
            )
            starts_down = (r == 0 or pattern[r - 1][c] == "#") and (
                r + 1 < GRID_SIZE and pattern[r + 1][c] == "."
            )

            if not starts_across and not starts_down:
                continue

            num = current_num
            current_num += 1
            numbering[r][c] = num

            if starts_across:
                length = 0
                cc = c
                while cc < GRID_SIZE and pattern[r][cc] == ".":
                    length += 1
                    cc += 1
                entries.append(
                    {
                        "number": num,
                        "direction": "across",
                        "row": r,
                        "col": c,
                        "length": length,
                    }
                )

            if starts_down:
                length = 0
                rr = r
                while rr < GRID_SIZE and pattern[rr][c] == ".":
                    length += 1
                    rr += 1
                entries.append(
                    {
                        "number": num,
                        "direction": "down",
                        "row": r,
                        "col": c,
                        "length": length,
                    }
                )

    entries.sort(key=lambda e: (e["number"], 0 if e["direction"] == "across" else 1))
    return {"numbering": numbering, "entries": entries}


def entry_cells(entry: dict) -> list[tuple[int, int]]:
    r = entry["row"]
    c = entry["col"]
    length = entry["length"]
    if entry["direction"] == "across":
        return [(r, c + i) for i in range(length)]
    return [(r + i, c) for i in range(length)]


def extract_entry_answer(solution: list[str], entry: dict) -> str:
    return "".join(solution[r][c] for r, c in entry_cells(entry))

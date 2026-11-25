from datetime import date, datetime, timedelta, timezone as dt_timezone
import time

from django.test import TestCase

from apps.games.crossword import (
    DEFAULT_AUTO_PATTERNS,
    auto_generate,
    check_cells,
    draft_clues,
    generate_fill,
    get_allowed_words,
    get_blocklist,
    is_solved,
    number_grid,
    reveal_cell,
    share_tokens,
    stats_for,
    validate_pattern,
    validate_solution,
)
from apps.games.models import ClueBank, CrosswordWord, MiniAttempt, MiniCrossword
from apps.story.models import Author, Story

MOCK_PATTERN = ["....#", ".....", ".....", ".....", "#...."]
MOCK_SOLUTION = ["SPAM#", "TALES", "AGILE", "RAVEN", "#NEED"]
MOCK_CLUES = [
    {"number": 1, "direction": "across", "answer": "SPAM", "clue": "Unwanted inbox messages", "story_id": None},
    {"number": 5, "direction": "across", "answer": "TALES", "clue": "Short stories of wonder", "story_id": None},
    {"number": 7, "direction": "across", "answer": "AGILE", "clue": "Quick and nimble", "story_id": None},
    {"number": 8, "direction": "across", "answer": "RAVEN", "clue": "Poe's midnight bird", "story_id": None},
    {"number": 9, "direction": "across", "answer": "NEED", "clue": "Strong requirement", "story_id": None},
    {"number": 1, "direction": "down", "answer": "STAR", "clue": "Bright night-sky point", "story_id": None},
    {"number": 2, "direction": "down", "answer": "PAGAN", "clue": "Ancient polytheistic belief", "story_id": None},
    {"number": 3, "direction": "down", "answer": "ALIVE", "clue": "Breathing and well", "story_id": None},
    {"number": 4, "direction": "down", "answer": "MELEE", "clue": "Chaotic close-quarters brawl", "story_id": None},
    {"number": 6, "direction": "down", "answer": "SEND", "clue": "Dispatch a letter", "story_id": None},
]


class CrosswordServicesTestCase(TestCase):
    def test_validate_pattern_accepts_valid_and_rejects_short_runs_and_isolated(self):
        self.assertEqual(validate_pattern(MOCK_PATTERN), [])
        for pat in DEFAULT_AUTO_PATTERNS:
            self.assertEqual(validate_pattern(pat), [])

        # 2-letter across run at row 0 cols 0-1
        bad_two = ["..###", ".....", ".....", ".....", "....."]
        errs = validate_pattern(bad_two)
        self.assertTrue(any("across run of length 2" in e for e in errs))

        # Isolated white cell at (0,0)
        isolated = [".####", "#####", ".....", ".....", "....."]
        errs2 = validate_pattern(isolated)
        self.assertTrue(len(errs2) >= 2)

        # Too few white cells
        sparse = ["###..", "###..", "###..", "#####", "#####"]
        errs3 = validate_pattern(sparse)
        self.assertTrue(any("at least 15" in e for e in errs3))

    def test_number_grid_matches_expected_positions(self):
        res = number_grid(MOCK_PATTERN)
        numbering = res["numbering"]
        # Row 0: 1, 2, 3, 4, None
        self.assertEqual(numbering[0], [1, 2, 3, 4, None])
        # Row 1: 5, None, None, None, 6
        self.assertEqual(numbering[1], [5, None, None, None, 6])
        # Row 2: 7 at col 0
        self.assertEqual(numbering[2][0], 7)
        # Row 3: 8 at col 0
        self.assertEqual(numbering[3][0], 8)
        # Row 4: None, 9, None, None, None
        self.assertEqual(numbering[4], [None, 9, None, None, None])

        entries = res["entries"]
        self.assertEqual(len(entries), 10)
        # Ordered by number then across before down
        self.assertEqual(entries[0]["number"], 1)
        self.assertEqual(entries[0]["direction"], "across")
        self.assertEqual(entries[1]["number"], 1)
        self.assertEqual(entries[1]["direction"], "down")

    def test_blocklist_always_wins_over_db_words(self):
        blocklist = get_blocklist()
        self.assertTrue(len(blocklist) > 20)
        sample_blocked = next(iter(blocklist))
        CrosswordWord.objects.create(word=sample_blocked)
        allowed = get_allowed_words(include_db=True)
        self.assertNotIn(sample_blocked, allowed)

        # Library words from published stories/authors are included
        author = Author.objects.create(name="Zorgon", slug="zorgon")
        Story.objects.create(title="Quxxy", slug="quxxy", author=author, is_published=True)
        allowed2 = get_allowed_words(include_db=True)
        self.assertIn("QUXXY", allowed2)

    def test_validate_solution_checks_self_leak_duplicates_and_warnings(self):
        res = validate_solution(MOCK_PATTERN, MOCK_SOLUTION, MOCK_CLUES)
        self.assertEqual(res["errors"], [])

        # Clue containing its own answer rejected
        bad_clues = [dict(c) for c in MOCK_CLUES]
        bad_clues[3]["clue"] = "A dark raven perched above the door"
        res_bad = validate_solution(MOCK_PATTERN, MOCK_SOLUTION, bad_clues)
        self.assertTrue(any("contains its own answer" in e for e in res_bad["errors"]))

        # Recently reused answer produces warning (non-blocking)
        MiniCrossword.objects.create(
            date=date.today() - timedelta(days=5),
            pattern=MOCK_PATTERN,
            solution=MOCK_SOLUTION,
            clues=MOCK_CLUES,
            status="approved",
        )
        res_warn = validate_solution(MOCK_PATTERN, MOCK_SOLUTION, MOCK_CLUES)
        self.assertEqual(res_warn["errors"], [])
        self.assertTrue(len(res_warn["warnings"]) > 0)

    def test_generate_fill_is_deterministic_and_time_boxed(self):
        t0 = time.monotonic()
        sols1 = generate_fill(MOCK_PATTERN, seed=20261125, limit=2, timeout_seconds=2.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.5)
        self.assertTrue(len(sols1) >= 1)

        sols2 = generate_fill(MOCK_PATTERN, seed=20261125, limit=2, timeout_seconds=2.0)
        self.assertEqual(sols1, sols2)

        # Ensure zero blocklisted words in generated solution
        blocklist = get_blocklist()
        for entry in number_grid(MOCK_PATTERN)["entries"]:
            r, c, length = entry["row"], entry["col"], entry["length"]
            word = (
                sols1[0][r][c : c + length]
                if entry["direction"] == "across"
                else "".join(sols1[0][r + i][c] for i in range(length))
            )
            self.assertNotIn(word, blocklist)

    def test_check_cells_reveal_share_tokens_and_stats(self):
        puzzle = MiniCrossword.objects.create(
            date=date.today(),
            title="Poe and Gothic",
            pattern=MOCK_PATTERN,
            solution=MOCK_SOLUTION,
            clues=MOCK_CLUES,
            status="approved",
        )
        attempt = MiniAttempt.objects.create(
            puzzle=puzzle,
            guest_key="guest-mini-1",
            letters=["SPXM#", "T....", ".....", ".....", "#...."],
            revealed=[],
            status="in_progress",
            is_archive=False,
        )

        # Check word at (0,0) across -> only (0,2) is wrong; empty cells not wrong
        wrong_word = check_cells(puzzle, attempt, "word", 0, 0, "across")
        self.assertEqual(wrong_word, [[0, 2]])
        wrong_grid = check_cells(puzzle, attempt, "grid")
        self.assertEqual(wrong_grid, [[0, 2]])

        # Reveal cell increments hints_used once per cell
        letter = reveal_cell(puzzle, attempt, 0, 2)
        self.assertEqual(letter, "A")
        self.assertEqual(attempt.hints_used, 1)
        reveal_cell(puzzle, attempt, 0, 2)
        self.assertEqual(attempt.hints_used, 1)

        # Share tokens contain ONLY G, Y, K — never letters
        tokens = share_tokens(puzzle, attempt)
        self.assertEqual(len(tokens), 5)
        self.assertEqual(tokens[0], "GGYGK")
        self.assertEqual(tokens[4][0], "K")
        for row in tokens:
            self.assertTrue(set(row).issubset({"G", "Y", "K"}))

        # Exact match check
        self.assertFalse(is_solved(puzzle, attempt.letters))
        self.assertTrue(is_solved(puzzle, MOCK_SOLUTION))

        # Mark solved with hints -> streak still counts!
        attempt.letters = MOCK_SOLUTION
        attempt.status = "solved"
        attempt.completed_at = datetime.now(dt_timezone.utc)
        attempt.elapsed_seconds = 65
        attempt.save()

        st = stats_for(guest_key="guest-mini-1")
        self.assertEqual(st["played"], 1)
        self.assertEqual(st["solved"], 1)
        self.assertEqual(st["current_streak"], 1)
        self.assertEqual(st["best_time"], 65)

    def test_draft_clues_and_auto_generate_stays_draft(self):
        ClueBank.objects.create(answer="RAVEN", clue="Nevermore bird", times_used=0)
        ClueBank.objects.create(answer="RAVEN", clue="Older clue", times_used=5)
        drafted = draft_clues(MOCK_SOLUTION, MOCK_PATTERN)
        raven_entry = next(c for c in drafted if c["answer"] == "RAVEN")
        self.assertEqual(raven_entry["clue"], "Nevermore bird")

        start = date(2026, 12, 1)
        res = auto_generate(start, days=2)
        self.assertEqual(res["created_count"], 2)
        for p in MiniCrossword.objects.filter(date__gte=start):
            self.assertEqual(p.status, MiniCrossword.STATUS_DRAFT)
            self.assertTrue(p.auto_generated)

        # Idempotent second run
        res2 = auto_generate(start, days=2)
        self.assertEqual(res2["created_count"], 0)

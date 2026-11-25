from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.games.models import DailyPuzzle, PuzzleAttempt
from apps.games.services import (
    author_initials,
    auto_fill,
    clues_for,
    compare_guess,
    compute_difficulty,
    era_of,
    extract_candidates,
    length_label,
    opening_line_in_story,
    result_share_text,
    stats_for,
)
from apps.story.models import Author, Chapter, Genre, Story

User = get_user_model()


class GameServicesTests(TestCase):
    def setUp(self):
        self.author_poe = Author.objects.create(name="Edgar Allan Poe")
        self.author_austen = Author.objects.create(name="Jane Austen")
        self.genre_horror = Genre.objects.create(name="Horror")
        self.genre_romance = Genre.objects.create(name="Romance")

        self.story_poe = Story.objects.create(
            title="The Tell-Tale Heart",
            slug="the-tell-tale-heart",
            author=self.author_poe,
            country="US",
            original_published_year=1843,
            views=600,
            is_published=True,
        )
        self.story_poe.genres.add(self.genre_horror)
        self.ch_poe = Chapter.objects.create(
            story=self.story_poe,
            title="Part 1",
            slug="part-1",
            order=1,
            content=(
                "<h2>CHAPTER I</h2><p>IV.</p><p>THE MADMAN</p>"
                "<p>True! — nervous, very, very dreadfully nervous I had been and am; "
                "but why will you say that I am mad? The disease had sharpened my senses, "
                "not destroyed, not dulled them.</p>"
            ),
        )

        self.story_austen = Story.objects.create(
            title="Pride and Prejudice",
            slug="pride-and-prejudice",
            author=self.author_austen,
            country="GB",
            original_published_year=1813,
            views=50,
            is_published=True,
        )
        self.story_austen.genres.add(self.genre_romance)
        Chapter.objects.create(
            story=self.story_austen,
            title="Chapter 1",
            slug="ch-1",
            order=1,
            content="<p>It is a truth universally acknowledged, that a single man in possession of a good fortune, must be in want of a wife.</p>",
        )

        self.story_minimal = Story.objects.create(
            title="Untitled Fragment",
            slug="untitled-fragment",
            author=None,
            country="",
            original_published_year=None,
            views=0,
            is_published=True,
        )
        Chapter.objects.create(
            story=self.story_minimal,
            title="Only",
            slug="only",
            order=1,
            content="<p>Short opening sentence that is still long enough to be picked.</p>",
        )

    def test_era_of(self):
        self.assertEqual(era_of(1843), "1840s")
        self.assertEqual(era_of(1900), "1900s")
        self.assertIsNone(era_of(None))
        self.assertIsNone(era_of(0))

    def test_author_initials(self):
        self.assertEqual(author_initials(self.story_poe), "E. A. P.")
        self.assertEqual(author_initials(self.story_austen), "J. A.")
        self.assertIsNone(author_initials(self.story_minimal))

    def test_length_label(self):
        self.assertEqual(length_label(self.story_poe), "Short story")
        novella_story = Story.objects.create(title="Mid", slug="mid", is_published=True)
        Chapter.objects.create(
            story=novella_story,
            title="Ch 1",
            slug="c1",
            order=1,
            content="<p>" + ("word " * 15000) + "</p>",
        )
        self.assertEqual(length_label(novella_story), "Novella")

        novel_story = Story.objects.create(title="Long", slug="long", is_published=True)
        Chapter.objects.create(
            story=novel_story,
            title="Ch 1",
            slug="c1",
            order=1,
            content="<p>" + ("word " * 41000) + "</p>",
        )
        self.assertEqual(length_label(novel_story), "Novel")

    def test_compare_guess_and_missing_fields(self):
        same = compare_guess(self.story_poe, self.story_poe)
        self.assertTrue(same["correct"])
        self.assertEqual(same["matches"], {"genre": True, "country": True, "era": "same"})

        close_story = Story.objects.create(
            title="Close Era",
            slug="close-era",
            country="US",
            original_published_year=1851,
            is_published=True,
        )
        close_story.genres.add(self.genre_romance)
        res_close = compare_guess(self.story_poe, close_story)
        self.assertFalse(res_close["correct"])
        self.assertEqual(res_close["matches"], {"genre": False, "country": True, "era": "close"})

        res_missing = compare_guess(self.story_minimal, self.story_poe)
        self.assertEqual(res_missing["matches"], {"genre": False, "country": False, "era": "different"})

    def test_clues_for_reveals_strictly_in_order(self):
        puzzle = DailyPuzzle.objects.create(
            date=date(2026, 11, 20),
            story=self.story_poe,
            opening_line="True! Nervous, very, very dreadfully nervous I had been and am.",
            source_chapter=self.ch_poe,
        )
        c1 = clues_for(puzzle, 1)
        self.assertEqual([c["key"] for c in c1], ["genre"])
        self.assertEqual(c1[0]["value"], "Horror")

        c5 = clues_for(puzzle, 5)
        self.assertEqual([c["key"] for c in c5], ["genre", "country", "era", "length", "author_initials"])
        self.assertEqual(c5[4]["value"], "E. A. P.")

        c6 = clues_for(puzzle, 6)
        self.assertEqual(len(c6), 6)
        self.assertEqual(c6[5]["key"], "cover")

    def test_extract_candidates_skips_headings_and_prefers_good_lines(self):
        cands = extract_candidates(self.story_poe, limit=5)
        self.assertGreaterEqual(len(cands), 1)
        self.assertNotIn("CHAPTER I", cands[0]["text"])
        self.assertNotIn("THE MADMAN", cands[0]["text"])
        self.assertTrue(cands[0]["text"].startswith("True!"))
        self.assertTrue(opening_line_in_story(self.story_poe, cands[0]["text"]))
        self.assertFalse(opening_line_in_story(self.story_poe, "Completely invented sentence."))

    def test_compute_difficulty(self):
        self.assertEqual(compute_difficulty("Short line here.", self.story_austen), "easy")
        self.assertEqual(compute_difficulty(" ".join(["word"] * 20), self.story_poe), "easy")  # popular views=600
        self.assertEqual(compute_difficulty(" ".join(["word"] * 20), self.story_austen), "medium")
        self.assertEqual(compute_difficulty(" ".join(["word"] * 35), self.story_austen), "hard")

    def test_auto_fill_deterministic_and_idempotent(self):
        start = date(2026, 12, 1)
        res1 = auto_fill(start, 3)
        self.assertEqual(len(res1["created"]), 3)
        self.assertEqual(len(res1["skipped"]), 0)

        res2 = auto_fill(start, 3)
        self.assertEqual(len(res2["created"]), 0)
        self.assertEqual(len(res2["skipped"]), 3)

    def test_result_share_text_and_stats_streak_excludes_archive(self):
        user = User.objects.create_user(email="player@example.com", username="player1", password="pw")
        d1 = date(2026, 11, 23)
        d2 = date(2026, 11, 24)
        d3 = date(2026, 11, 25)
        p1 = DailyPuzzle.objects.create(date=d1, story=self.story_poe, opening_line="Line one.")
        p2 = DailyPuzzle.objects.create(date=d2, story=self.story_austen, opening_line="Line two.")
        p3 = DailyPuzzle.objects.create(date=d3, story=self.story_minimal, opening_line="Line three.")

        # Solved p1 as archive on d3 -> must NOT count toward streak
        PuzzleAttempt.objects.create(
            puzzle=p1,
            user=user,
            guesses=[{"story_id": self.story_poe.id, "correct": True, "matches": {}, "skipped": False}],
            status=PuzzleAttempt.STATUS_SOLVED,
            guess_count=1,
            completed_at=datetime(2026, 11, 25, 10, 0, tzinfo=dt_timezone.utc),
        )
        # Solved p2 on d2 and p3 on d3 -> streak = 2
        a2 = PuzzleAttempt.objects.create(
            puzzle=p2,
            user=user,
            guesses=[
                {"story_id": self.story_poe.id, "correct": False, "matches": {}, "skipped": False},
                {"story_id": self.story_austen.id, "correct": True, "matches": {}, "skipped": False},
            ],
            status=PuzzleAttempt.STATUS_SOLVED,
            guess_count=2,
            completed_at=datetime(2026, 11, 24, 12, 0, tzinfo=dt_timezone.utc),
        )
        PuzzleAttempt.objects.create(
            puzzle=p3,
            user=user,
            guesses=[{"story_id": self.story_minimal.id, "correct": True, "matches": {}, "skipped": False}],
            status=PuzzleAttempt.STATUS_SOLVED,
            guess_count=1,
            completed_at=datetime(2026, 11, 25, 12, 0, tzinfo=dt_timezone.utc),
        )

        share = result_share_text(p2, a2)
        self.assertEqual(share["tokens"], ["wrong", "right", "empty", "empty", "empty", "empty"])
        self.assertNotIn("Pride", share["header"])

        stats = stats_for(user=user, today=d3)
        self.assertEqual(stats["played"], 3)
        self.assertEqual(stats["wins"], 3)
        self.assertEqual(stats["win_percentage"], 100)
        self.assertEqual(stats["current_streak"], 2)
        self.assertEqual(stats["best_streak"], 2)
        self.assertEqual(stats["guess_distribution"]["1"], 2)
        self.assertEqual(stats["guess_distribution"]["2"], 1)

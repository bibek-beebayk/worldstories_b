from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleRotation
from apps.games.services import (
    DEFAULT_ROTATION,
    AuthorBioBuilder,
    AuthorResolver,
    OpeningLineBuilder,
    QuoteBuilder,
    StoryResolver,
    TriviaBuilder,
    auto_fill,
    clues_for,
    compare_years,
    compute_difficulty,
    decade_label,
    format_initials,
    html_to_plain_text,
    leak_check,
    result_share_tokens,
    rotation_for_weekday,
    split_sentences,
    stats_for,
    validate_for_approval,
)
from apps.story.models import Author, Chapter, Genre, Story

User = get_user_model()


class GamesServicesTestCase(TestCase):
    def setUp(self):
        self.g_gothic = Genre.objects.create(name="Gothic", slug="gothic")
        self.g_mystery = Genre.objects.create(name="Mystery", slug="mystery")
        self.g_scifi = Genre.objects.create(name="Sci-Fi", slug="sci-fi")

        self.author_poe = Author.objects.create(
            name="Edgar Allan Poe",
            slug="edgar-allan-poe",
            country="US",
            birth_year=1809,
            death_year=1849,
        )
        self.author_shelley = Author.objects.create(
            name="Mary Shelley",
            slug="mary-shelley",
            country="",  # Intentionally blank to test derivation from stories
            birth_year=None,
        )
        self.author_single = Author.objects.create(
            name="Single Story Writer",
            slug="single-story-writer",
            country="FR",
            birth_year=1920,
        )

        self.story_raven = Story.objects.create(
            title="The Murders in the Rue Morgue",
            slug="the-murders-in-the-rue-morgue",
            author=self.author_poe,
            country="US",
            original_published_year=1841,
            is_published=True,
            views=800,
        )
        self.story_raven.genres.add(self.g_gothic, self.g_mystery)
        Chapter.objects.create(
            story=self.story_raven,
            title="Chapter 1",
            order=1,
            content=(
                "<h2>CHAPTER I</h2><p>IV.</p>"
                "<p>THE MENTAL FEATURES DISCOURSED OF AS THE ANALYTICAL.</p>"
                "<p>Residing in Paris during the spring and part of the summer of 1800, "
                "I there became acquainted with a Monsieur Dupin.</p>"
                "<p>He was a young gentleman of an excellent and illustrious family.</p>"
            ),
        )
        Chapter.objects.create(
            story=self.story_raven,
            title="Chapter 2",
            order=2,
            content=(
                "<p>The police were confounded by the seeming impossibility of the locked room "
                "and the extraordinary strength displayed within the chamber.</p>"
                "<p>A second paragraph discussed the Murders in great detail.</p>"
            ),
        )
        Chapter.objects.create(
            story=self.story_raven,
            title="Chapter 3",
            order=3,
            content=(
                "<p>In the end, the sailor confessed everything to the quiet investigator "
                "sitting by the lamplight.</p>"
            ),
        )

        self.story_poe_2 = Story.objects.create(
            title="The Tell-Tale Heart",
            slug="the-tell-tale-heart",
            author=self.author_poe,
            country="US",
            original_published_year=1843,
            is_published=True,
            views=600,
        )
        self.story_poe_2.genres.add(self.g_gothic)
        Chapter.objects.create(
            story=self.story_poe_2,
            title="Chapter 1",
            order=1,
            content=(
                "<p>True! Nervous, very, very dreadfully nervous I had been and am, "
                "but why will you say that I am mad?</p>"
                "<p>The disease had sharpened my senses, not destroyed, not dulled them at all.</p>"
            ),
        )

        self.story_frank = Story.objects.create(
            title="Frankenstein",
            slug="frankenstein",
            author=self.author_shelley,
            country="GB",
            original_published_year=1818,
            is_published=True,
            views=400,
        )
        self.story_frank.genres.add(self.g_gothic, self.g_scifi)
        Chapter.objects.create(
            story=self.story_frank,
            title="Letter 1",
            order=1,
            content=(
                "<p>You will rejoice to hear that no disaster has accompanied the commencement "
                "of an enterprise which you have regarded with such evil forebodings.</p>"
            ),
        )
        self.story_frank_2 = Story.objects.create(
            title="The Last Man",
            slug="the-last-man",
            author=self.author_shelley,
            country="GB",
            original_published_year=1826,
            is_published=True,
            views=200,
        )
        self.story_frank_2.genres.add(self.g_scifi)

        # Edge cases: story with no author, missing year, no genres, tiny chapter
        self.story_bare = Story.objects.create(
            title="Untitled Fragment",
            slug="untitled-fragment",
            author=None,
            country="",
            original_published_year=None,
            is_published=True,
            views=10,
        )
        Chapter.objects.create(
            story=self.story_bare,
            title="Ch 1",
            order=1,
            content="<p>Hi.</p>",
        )

        # Single-story author
        self.story_single = Story.objects.create(
            title="One Solo Work",
            slug="one-solo-work",
            author=self.author_single,
            country="FR",
            original_published_year=1948,
            is_published=True,
            views=100,
        )
        self.story_single.genres.add(self.g_mystery)

    def test_text_helpers(self):
        plain = html_to_plain_text("<h1>CHAPTER I</h1><p>IV.</p><p>It was a dark &amp; stormy night.</p>")
        sentences = split_sentences(plain)
        self.assertEqual(sentences, ["It was a dark & stormy night."])
        self.assertEqual(format_initials("Edgar Allan Poe"), "E. A. P.")
        self.assertEqual(decade_label(1841), "1840s")
        self.assertIsNone(decade_label(None))

    def test_compare_years(self):
        self.assertEqual(compare_years(1841, 1849), "same")
        self.assertEqual(compare_years(1849, 1852), "close")
        self.assertEqual(compare_years(1841, 1890), "different")
        self.assertEqual(compare_years(1841, None), "different")

    def test_story_resolver_attributes_and_compare(self):
        resolver = StoryResolver()
        attrs = resolver.attributes(self.story_raven)
        self.assertEqual(attrs["genre"], "Gothic, Mystery")
        self.assertEqual(attrs["country"], "United States")
        self.assertEqual(attrs["era"], "1840s")
        self.assertEqual(attrs["length"], "Short story")
        self.assertEqual(attrs["initials"], "E. A. P.")

        cmp_same = resolver.compare(self.story_raven, self.story_raven)
        self.assertTrue(cmp_same["correct"])
        self.assertTrue(cmp_same["matches"]["genre"])
        self.assertTrue(cmp_same["matches"]["country"])
        self.assertEqual(cmp_same["matches"]["era"], "same")

        cmp_other = resolver.compare(self.story_raven, self.story_frank)
        self.assertFalse(cmp_other["correct"])
        self.assertTrue(cmp_other["matches"]["genre"])  # both Gothic
        self.assertFalse(cmp_other["matches"]["country"])
        self.assertEqual(cmp_other["matches"]["era"], "different")

        # Missing values never match
        cmp_bare = resolver.compare(self.story_bare, self.story_bare)
        self.assertTrue(cmp_bare["correct"])
        self.assertFalse(cmp_bare["matches"]["genre"])
        self.assertFalse(cmp_bare["matches"]["country"])
        self.assertEqual(cmp_bare["matches"]["era"], "different")

    def test_author_resolver_derives_missing_country_era_genres(self):
        resolver = AuthorResolver()
        attrs = resolver.attributes(self.author_shelley)
        # Derived from Frankenstein (GB, 1818) & The Last Man (GB, 1826)
        self.assertEqual(attrs["country"], "United Kingdom")
        self.assertEqual(attrs["era"], "1810s")
        self.assertEqual(attrs["genre"], "Gothic, Sci-Fi")
        self.assertEqual(attrs["works_count"], "2 stories on WorldStories")
        self.assertEqual(attrs["works_sample"], "Frankenstein")

        # Author with explicit birth_year/country uses them directly
        poe_attrs = resolver.attributes(self.author_poe)
        self.assertEqual(poe_attrs["country"], "United States")
        self.assertEqual(poe_attrs["era"], "1800s")

        # Close era comparison: 1809 vs 1818 -> diff 9 -> close
        cmp_authors = resolver.compare(self.author_poe, self.author_shelley)
        self.assertFalse(cmp_authors["correct"])
        self.assertTrue(cmp_authors["matches"]["genre"])
        self.assertFalse(cmp_authors["matches"]["country"])
        self.assertEqual(cmp_authors["matches"]["era"], "close")

    def test_opening_line_and_quote_builders_filter_headings_and_leaks(self):
        ob = OpeningLineBuilder()
        cands = ob.candidates(self.story_raven)
        self.assertTrue(len(cands) >= 1)
        self.assertTrue(cands[0]["text"].startswith("Residing in Paris"))

        qb = QuoteBuilder()
        q_cands = qb.candidates(self.story_raven)
        self.assertTrue(len(q_cands) >= 1)
        # Sentence containing "Murders" (title word) must be filtered out by leak check
        for c in q_cands:
            self.assertNotIn("Murders", c["text"])

        # Tiny chapter yields no candidates
        self.assertEqual(ob.candidates(self.story_bare), [])
        self.assertEqual(TriviaBuilder().can_auto_build, False)
        self.assertEqual(TriviaBuilder().default_clues(self.story_raven), [])

    def test_leak_check_and_validation(self):
        leaks = leak_check(
            {
                "puzzle_type": "trivia",
                "answer_kind": "story",
                "answer": self.story_raven,
                "clues": [
                    {"kind": "text", "label": "Clue 1", "value": "Set in Paris with a clever detective."},
                    {"kind": "text", "label": "Clue 2", "value": "Written by Poe about famous murders."},
                    {"kind": "attribute", "key": "genre", "label": "Genre"},
                    {"kind": "attribute", "key": "country", "label": "Country"},
                ],
            }
        )
        leaked_terms = {item["term"] for item in leaks}
        self.assertIn("poe", leaked_terms)
        self.assertIn("murders", leaked_terms)

        # Author leak check catches 3+ letter name tokens (and accented forms)
        author_leaks = leak_check(
            {
                "puzzle_type": "author_bio",
                "answer_kind": "author",
                "answer": self.author_poe,
                "clues": [
                    {"kind": "text", "label": "Bio", "value": "Born in Boston, Édgar wrote macabre tales."},
                ],
            }
        )
        self.assertTrue(any(item["term"] == "edgar" for item in author_leaks))

        # Validation passes for clean opening_line default clues
        clues = OpeningLineBuilder().default_clues(self.story_raven)
        errors = validate_for_approval(
            {
                "puzzle_type": "opening_line",
                "answer_kind": "story",
                "answer": self.story_raven,
                "clues": clues,
            }
        )
        self.assertEqual(errors, [])

        # Validation fails for bare story with unresolvable attributes
        bare_errors = validate_for_approval(
            {
                "puzzle_type": "opening_line",
                "answer_kind": "story",
                "answer": self.story_bare,
                "clues": [
                    {"kind": "text", "label": "Opening line", "value": "A mysterious opening sentence."},
                    {"kind": "attribute", "key": "genre", "label": "Genre"},
                    {"kind": "attribute", "key": "country", "label": "Country"},
                    {"kind": "attribute", "key": "era", "label": "Era"},
                ],
            }
        )
        self.assertTrue(len(bare_errors) >= 3)

    def test_clues_for_hides_locked_clues(self):
        clues = OpeningLineBuilder().default_clues(self.story_raven)
        puzzle = DailyPuzzle.objects.create(
            date=date(2026, 11, 20),
            puzzle_type="opening_line",
            answer_kind="story",
            answer_story=self.story_raven,
            clues=clues,
            status="approved",
        )
        res = clues_for(puzzle, unlocked_count=2)
        self.assertEqual(len(res), 6)
        self.assertFalse(res[0]["locked"])
        self.assertFalse(res[1]["locked"])
        self.assertEqual(res[1]["value"], "Gothic, Mystery")
        for locked_item in res[2:]:
            self.assertEqual(set(locked_item.keys()), {"position", "locked"})
            self.assertTrue(locked_item["locked"])

    def test_auto_fill_respects_rotation_and_skips_trivia(self):
        # 2026-11-23 is a Monday (weekday 0 -> opening_line, Tue -> trivia, Wed -> author_bio, Thu -> quote)
        start = date(2026, 11, 23)
        self.assertEqual(rotation_for_weekday(0), DEFAULT_ROTATION[0])
        summary = auto_fill(start, days=4)
        # Tuesday (2026-11-24) is trivia -> skipped_empty
        self.assertIn("2026-11-24", summary["skipped_empty"])
        self.assertTrue(DailyPuzzle.objects.filter(date=date(2026, 11, 23), puzzle_type="opening_line").exists())
        self.assertTrue(DailyPuzzle.objects.filter(date=date(2026, 11, 25), puzzle_type="author_bio").exists())
        # Single-story author should NOT be auto-picked for author_bio
        wed = DailyPuzzle.objects.get(date=date(2026, 11, 25))
        self.assertNotEqual(wed.answer_author_id, self.author_single.id)

        # Running auto_fill a second time is idempotent
        summary2 = auto_fill(start, days=4)
        self.assertEqual(summary2["created_count"], 0)
        self.assertEqual(summary2["drafted_count"], 0)

    def test_stats_and_share_tokens(self):
        clues = AuthorBioBuilder().default_clues(self.author_poe)
        today = date.today()
        puzzle = DailyPuzzle.objects.create(
            date=today,
            puzzle_type="author_bio",
            answer_kind="author",
            answer_author=self.author_poe,
            clues=clues,
            status="approved",
        )
        attempt = PuzzleAttempt.objects.create(
            puzzle=puzzle,
            guest_key="guest-abc",
            guesses=[
                {"target_id": self.author_shelley.id, "correct": False, "matches": {}, "skipped": False},
                {"target_id": self.author_poe.id, "correct": True, "matches": {}, "skipped": False},
            ],
            status="solved",
            guess_count=2,
            completed_at=datetime.now(dt_timezone.utc),
        )
        tokens = result_share_tokens(puzzle, attempt)
        self.assertEqual(tokens, ["wrong", "right", "empty", "empty", "empty", "empty"])

        st = stats_for(guest_key="guest-abc")
        self.assertEqual(st["played"], 1)
        self.assertEqual(st["wins"], 1)
        self.assertEqual(st["win_percentage"], 100)
        self.assertEqual(st["guess_distribution"]["2"], 1)
        self.assertIn(compute_difficulty(puzzle), ("easy", "medium", "hard"))

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from rest_framework.test import APITestCase

from apps.games.models import DailyPuzzle, PuzzleAttempt
from apps.games.services import utc_today
from apps.story.models import Author, Chapter, Genre, Story

User = get_user_model()


class NameThatStoryApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="nts-admin@example.com", username="nts_admin", password="pw"
        )
        self.user = User.objects.create_user(
            email="nts-user@example.com", username="nts_user", password="pw"
        )
        self.author_a = Author.objects.create(name="Edgar Allan Poe")
        self.author_b = Author.objects.create(name="Mary Shelley")
        self.g_horror = Genre.objects.create(name="Horror")
        self.g_scifi = Genre.objects.create(name="Science Fiction")

        self.answer_story = Story.objects.create(
            title="The Black Cat",
            slug="the-black-cat",
            author=self.author_a,
            country="US",
            original_published_year=1843,
            cover_image="https://cdn.example.com/cat.jpg",
            is_published=True,
        )
        self.answer_story.genres.add(self.g_horror)
        self.ch_answer = Chapter.objects.create(
            story=self.answer_story,
            title="One",
            slug="one",
            order=1,
            content="<p>For the most wild, yet most homely narrative which I am about to pen, I neither expect nor solicit belief.</p>",
        )

        self.wrong_stories = []
        for idx in range(7):
            s = Story.objects.create(
                title=f"Decoy Story {idx}",
                slug=f"decoy-story-{idx}",
                author=self.author_b,
                country="GB",
                original_published_year=1818,
                is_published=True,
            )
            s.genres.add(self.g_scifi)
            Chapter.objects.create(
                story=s,
                title="Ch 1",
                slug="ch-1",
                order=1,
                content=f"<p>Decoy opening sentence number {idx} with plenty of words for testing purposes.</p>",
            )
            self.wrong_stories.append(s)

        self.today = utc_today()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow = self.today + timedelta(days=1)

        self.puzzle_yesterday = DailyPuzzle.objects.create(
            date=self.yesterday,
            story=self.wrong_stories[0],
            opening_line="Decoy opening sentence number 0 with plenty of words for testing purposes.",
        )
        self.puzzle_today = DailyPuzzle.objects.create(
            date=self.today,
            story=self.answer_story,
            opening_line="For the most wild, yet most homely narrative which I am about to pen, I neither expect nor solicit belief.",
            source_chapter=self.ch_answer,
        )

    def test_answer_never_leaked_before_completion_and_clues_unlock_in_order(self):
        headers = {"HTTP_X_GUEST_KEY": "guest-uuid-1"}
        res = self.client.get("/api/games/name-that-story/today/", **headers)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("answer", res.data)
        self.assertNotIn("share", res.data)
        self.assertEqual([c["key"] for c in res.data["clues"]], ["genre"])
        self.assertNotIn("The Black Cat", str(res.data))

        # Wrong guess 1 -> unlocks country
        res1 = self.client.post(
            "/api/games/name-that-story/guess/",
            {"story_id": self.wrong_stories[1].id},
            format="json",
            **headers,
        )
        self.assertEqual(res1.status_code, 200)
        self.assertNotIn("answer", res1.data)
        self.assertEqual([c["key"] for c in res1.data["clues"]], ["genre", "country"])
        self.assertEqual(res1.data["newly_unlocked_clue"]["key"], "country")

        # Duplicate guess rejected without consuming a guess
        dup = self.client.post(
            "/api/games/name-that-story/guess/",
            {"story_id": self.wrong_stories[1].id},
            format="json",
            **headers,
        )
        self.assertEqual(dup.status_code, 400)
        self.assertEqual(dup.data["error"], "already_guessed")

        # Skip consumes guess 2 -> unlocks era
        res2 = self.client.post("/api/games/name-that-story/skip/", {}, format="json", **headers)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.data["guesses_used"], 2)
        self.assertEqual([c["key"] for c in res2.data["clues"]], ["genre", "country", "era"])

        # Correct guess 3 -> solved, returns answer & share tokens
        res3 = self.client.post(
            "/api/games/name-that-story/guess/",
            {"story_id": self.answer_story.id},
            format="json",
            **headers,
        )
        self.assertEqual(res3.status_code, 200)
        self.assertEqual(res3.data["status"], "solved")
        self.assertEqual(res3.data["answer"]["slug"], "the-black-cat")
        self.assertEqual(res3.data["share"]["tokens"], ["wrong", "wrong", "right", "empty", "empty", "empty"])

        # Subsequent guess returns 409
        res4 = self.client.post(
            "/api/games/name-that-story/guess/",
            {"story_id": self.wrong_stories[2].id},
            format="json",
            **headers,
        )
        self.assertEqual(res4.status_code, 409)

    def test_six_wrong_guesses_fail_and_reveal_answer(self):
        headers = {"HTTP_X_GUEST_KEY": "guest-uuid-fail"}
        for i in range(6):
            res = self.client.post(
                "/api/games/name-that-story/guess/",
                {"story_id": self.wrong_stories[i].id},
                format="json",
                **headers,
            )
            self.assertEqual(res.status_code, 200)
            if i < 5:
                self.assertEqual(res.data["status"], "in_progress")
                self.assertNotIn("answer", res.data)
            else:
                self.assertEqual(res.data["status"], "failed")
                self.assertEqual(res.data["answer"]["title"], "The Black Cat")
                self.assertEqual(res.data["share"]["tokens"], ["wrong"] * 6)

    def test_future_date_404_and_archive_excludes_answers_and_streak(self):
        future = self.client.get(f"/api/games/name-that-story/puzzles/{self.tomorrow.isoformat()}/")
        self.assertEqual(future.status_code, 404)

        self.client.force_authenticate(user=self.user)
        # Play yesterday's archive puzzle today -> solved, but does NOT increase streak
        res = self.client.post(
            "/api/games/name-that-story/guess/",
            {"date": self.yesterday.isoformat(), "story_id": self.wrong_stories[0].id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "solved")

        stats = self.client.get("/api/games/name-that-story/stats/")
        self.assertEqual(stats.data["played"], 1)
        self.assertEqual(stats.data["wins"], 1)
        self.assertEqual(stats.data["current_streak"], 0)

        arch = self.client.get("/api/games/name-that-story/archive/")
        self.assertEqual(arch.status_code, 200)
        self.assertEqual(len(arch.data["results"]), 1)
        self.assertNotIn("answer", arch.data["results"][0])
        self.assertEqual(arch.data["results"][0]["status"], "solved")

    def test_guest_merge_is_idempotent(self):
        headers = {"HTTP_X_GUEST_KEY": "guest-merge-1"}
        self.client.post(
            "/api/games/name-that-story/guess/",
            {"story_id": self.answer_story.id},
            format="json",
            **headers,
        )
        self.client.force_authenticate(user=self.user)
        m1 = self.client.post(
            "/api/games/name-that-story/merge-guest/",
            {"guest_key": "guest-merge-1"},
            format="json",
        )
        self.assertEqual(m1.status_code, 200)
        self.assertEqual(m1.data, {"merged": 1, "skipped": 0})

        m2 = self.client.post(
            "/api/games/name-that-story/merge-guest/",
            {"guest_key": "guest-merge-1"},
            format="json",
        )
        self.assertEqual(m2.data, {"merged": 0, "skipped": 0})

    def test_admin_endpoints_permissions_validation_and_autofill_command(self):
        # Non-admin gets 403
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get("/api/admin/games/name-that-story/summary/").status_code, 403)

        # Admin creates puzzle with validation
        self.client.force_authenticate(user=self.admin)
        bad = self.client.post(
            "/api/admin/games/name-that-story/puzzles/",
            {
                "date": self.tomorrow.isoformat(),
                "story_id": self.answer_story.id,
                "opening_line": "Invented line not in text.",
            },
            format="json",
        )
        self.assertEqual(bad.status_code, 400)

        ok = self.client.post(
            "/api/admin/games/name-that-story/puzzles/",
            {
                "date": self.tomorrow.isoformat(),
                "story_id": self.answer_story.id,
                "opening_line": "Invented line not in text.",
                "allow_custom": True,
            },
            format="json",
        )
        self.assertEqual(ok.status_code, 201)

        # Deleting today's puzzle with attempts returns 409
        PuzzleAttempt.objects.create(puzzle=self.puzzle_today, user=self.user, guess_count=1)
        del_res = self.client.delete(f"/api/admin/games/name-that-story/puzzles/{self.puzzle_today.id}/")
        self.assertEqual(del_res.status_code, 409)

        # Autofill management command idempotent
        out1 = StringIO()
        call_command("autofill_puzzles", "--days", "3", stdout=out1)
        out2 = StringIO()
        call_command("autofill_puzzles", "--days", "3", stdout=out2)
        self.assertIn("created=0", out2.getvalue())

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleRotation
from apps.games.services import AuthorBioBuilder, OpeningLineBuilder, utc_today
from apps.story.models import Author, Chapter, Genre, Story

User = get_user_model()


class GamesAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.today = utc_today()

        self.user = User.objects.create_user(
            username="reader1", email="reader1@example.com", password="password123"
        )
        self.staff = User.objects.create_superuser(
            username="admin1", email="admin1@example.com", password="password123"
        )

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
            country="GB",
            birth_year=1812,
            death_year=1851,
        )
        self.author_verne = Author.objects.create(
            name="Jules Verne",
            slug="jules-verne",
            country="FR",
            birth_year=1828,
            death_year=1905,
        )

        self.story1 = Story.objects.create(
            title="The Black Cat",
            slug="the-black-cat",
            author=self.author_poe,
            country="US",
            original_published_year=1843,
            is_published=True,
            views=700,
        )
        self.story1.genres.add(self.g_gothic, self.g_mystery)
        Chapter.objects.create(
            story=self.story1,
            title="Chapter 1",
            order=1,
            content=(
                "<p>For the most wild, yet most homely narrative which I am about to pen, "
                "I neither expect nor solicit belief.</p>"
                "<p>Mad indeed would I be to expect it, in a case where my very senses reject their own evidence.</p>"
            ),
        )
        Chapter.objects.create(
            story=self.story1,
            title="Chapter 2",
            order=2,
            content=(
                "<p>From my infancy I was noted for the docility and humanity of my disposition "
                "across the quiet neighborhood where I grew up.</p>"
            ),
        )
        Chapter.objects.create(
            story=self.story1,
            title="Chapter 3",
            order=3,
            content=(
                "<p>Upon the night of the fire, the walls of the ruined house stood upright "
                "against the cold autumn sky.</p>"
            ),
        )

        self.story2 = Story.objects.create(
            title="Frankenstein",
            slug="frankenstein",
            author=self.author_shelley,
            country="GB",
            original_published_year=1818,
            is_published=True,
            views=650,
        )
        self.story2.genres.add(self.g_gothic, self.g_scifi)
        Chapter.objects.create(
            story=self.story2,
            title="Letter 1",
            order=1,
            content=(
                "<p>You will rejoice to hear that no disaster has accompanied the commencement "
                "of an enterprise which you have regarded with such evil forebodings.</p>"
            ),
        )

        self.story3 = Story.objects.create(
            title="Twenty Thousand Leagues",
            slug="twenty-thousand-leagues",
            author=self.author_verne,
            country="FR",
            original_published_year=1870,
            is_published=True,
            views=500,
        )
        self.story3.genres.add(self.g_scifi)
        Chapter.objects.create(
            story=self.story3,
            title="Ch 1",
            order=1,
            content=(
                "<p>The year 1866 was signalised by a remarkable incident, a mysterious and "
                "puzzling phenomenon, which doubtless no one has yet forgotten.</p>"
            ),
        )

        # Extra stories for multi-story authors (so auto_fill can pick them)
        self.story4 = Story.objects.create(
            title="The Pit and the Pendulum",
            slug="the-pit-and-the-pendulum",
            author=self.author_poe,
            country="US",
            original_published_year=1842,
            is_published=True,
            views=550,
        )
        self.story4.genres.add(self.g_gothic)
        Chapter.objects.create(
            story=self.story4,
            title="Ch 1",
            order=1,
            content="<p>I was sick, sick unto death with that long agony in the dark hall.</p>",
        )

        self.story5 = Story.objects.create(
            title="The Last Man",
            slug="the-last-man",
            author=self.author_shelley,
            country="GB",
            original_published_year=1826,
            is_published=True,
            views=300,
        )
        self.story5.genres.add(self.g_scifi)

        # Extra stories for 6 distinct wrong guesses
        self.extra_stories = []
        for i in range(6):
            st = Story.objects.create(
                title=f"Decoy Tale Number {i + 1}",
                slug=f"decoy-tale-{i + 1}",
                author=self.author_verne,
                country="DE",
                original_published_year=1900 + i,
                is_published=True,
                views=50,
            )
            st.genres.add(self.g_scifi)
            self.extra_stories.append(st)

        # Today: approved opening_line (story)
        self.today_puzzle = DailyPuzzle.objects.create(
            date=self.today,
            puzzle_type="opening_line",
            answer_kind="story",
            answer_story=self.story1,
            clues=OpeningLineBuilder().default_clues(self.story1),
            status="approved",
        )

        # Yesterday: approved author_bio (author)
        self.yesterday_puzzle = DailyPuzzle.objects.create(
            date=self.today - timedelta(days=1),
            puzzle_type="author_bio",
            answer_kind="author",
            answer_author=self.author_poe,
            clues=AuthorBioBuilder().default_clues(self.author_poe),
            status="approved",
        )

        # Draft puzzle (2 days ago) - must NEVER be served publicly
        self.draft_puzzle = DailyPuzzle.objects.create(
            date=self.today - timedelta(days=2),
            puzzle_type="opening_line",
            answer_kind="story",
            answer_story=self.story2,
            clues=OpeningLineBuilder().default_clues(self.story2),
            status="draft",
        )

        # Future approved puzzle - must 404 publicly
        self.future_puzzle = DailyPuzzle.objects.create(
            date=self.today + timedelta(days=1),
            puzzle_type="opening_line",
            answer_kind="story",
            answer_story=self.story3,
            clues=OpeningLineBuilder().default_clues(self.story3),
            status="approved",
        )

    def test_no_answer_or_locked_clue_leaked_before_completion_story_and_author(self):
        # Story puzzle
        resp = self.client.get("/api/games/name-that-story/today/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("answer", data)
        self.assertNotIn("share_tokens", data)
        self.assertEqual(len(data["clues"]), 6)
        self.assertFalse(data["clues"][0]["locked"])
        for c in data["clues"][1:]:
            self.assertTrue(c["locked"])
            self.assertEqual(set(c.keys()), {"position", "locked"})
        # Answer title must not appear in serialized body (excluding yesterday which is author_poe)
        body_no_yday = json.dumps({k: v for k, v in data.items() if k != "yesterday"})
        self.assertNotIn("The Black Cat", body_no_yday)
        self.assertNotIn("Gothic", body_no_yday)

        # Author puzzle (yesterday)
        yday = (self.today - timedelta(days=1)).isoformat()
        resp2 = self.client.get(f"/api/games/name-that-story/puzzles/{yday}/")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2["answer_kind"], "author")
        self.assertNotIn("answer", data2)
        self.assertFalse(data2["clues"][0]["locked"])
        for c in data2["clues"][1:]:
            self.assertTrue(c["locked"])
            self.assertEqual(set(c.keys()), {"position", "locked"})
        body2 = json.dumps(data2)
        self.assertNotIn("Edgar Allan Poe", body2)
        self.assertNotIn("United States", body2)

    def test_draft_and_future_puzzles_never_served(self):
        draft_date = (self.today - timedelta(days=2)).isoformat()
        future_date = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(
            self.client.get(f"/api/games/name-that-story/puzzles/{draft_date}/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/games/name-that-story/puzzles/{future_date}/").status_code,
            404,
        )

    def test_guess_flow_reveal_order_duplicate_skip_and_six_wrong_completion(self):
        headers = {"HTTP_X_GUEST_KEY": "guest-flow-1"}

        # 1. Wrong guess reveals clue 2
        r1 = self.client.post(
            "/api/games/name-that-story/guess/",
            {"target_id": self.extra_stories[0].id},
            format="json",
            **headers,
        )
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertEqual(d1["guesses_used"], 1)
        self.assertFalse(d1["clues"][1]["locked"])
        self.assertTrue(d1["clues"][2]["locked"])
        self.assertNotIn("answer", d1)

        # 2. Duplicate guess rejected without consuming a guess
        r_dup = self.client.post(
            "/api/games/name-that-story/guess/",
            {"target_id": self.extra_stories[0].id},
            format="json",
            **headers,
        )
        self.assertEqual(r_dup.status_code, 400)
        self.assertEqual(r_dup.json()["error"], "already_guessed")

        # 3. Skip consumes a guess and reveals clue 3
        r2 = self.client.post(
            "/api/games/name-that-story/skip/",
            {},
            format="json",
            **headers,
        )
        self.assertEqual(r2.status_code, 200)
        d2 = r2.json()
        self.assertEqual(d2["guesses_used"], 2)
        self.assertFalse(d2["clues"][2]["locked"])
        self.assertTrue(d2["clues"][3]["locked"])

        # 4. 4 more wrong guesses -> failed on 6th
        for idx in range(1, 5):
            r = self.client.post(
                "/api/games/name-that-story/guess/",
                {"target_id": self.extra_stories[idx].id},
                format="json",
                **headers,
            )
            self.assertEqual(r.status_code, 200)

        final_data = r.json()
        self.assertEqual(final_data["status"], "failed")
        self.assertEqual(final_data["guesses_used"], 6)
        self.assertIn("answer", final_data)
        self.assertEqual(final_data["answer"]["title"], "The Black Cat")
        self.assertEqual(final_data["share_tokens"], ["wrong"] * 6)

        # 5. Further guesses return 409
        r_after = self.client.post(
            "/api/games/name-that-story/guess/",
            {"target_id": self.story1.id},
            format="json",
            **headers,
        )
        self.assertEqual(r_after.status_code, 409)

    def test_author_puzzle_search_kind_enforcement_and_comparison(self):
        yday = (self.today - timedelta(days=1)).isoformat()
        # Even if client passes kind=story for an author puzzle date, server returns author kind
        r_search = self.client.get(
            f"/api/games/name-that-story/search/?q=Shelley&kind=story&date={yday}"
        )
        self.assertEqual(r_search.status_code, 200)
        self.assertEqual(r_search.json()["kind"], "author")
        self.assertEqual(r_search.json()["results"][0]["name"], "Mary Shelley")

        headers = {"HTTP_X_GUEST_KEY": "guest-author-1"}
        # Explicit mismatched kind rejected
        r_bad = self.client.post(
            "/api/games/name-that-story/guess/",
            {"date": yday, "target_id": self.story1.id, "kind": "story"},
            format="json",
            **headers,
        )
        self.assertEqual(r_bad.status_code, 400)

        # Guessing Mary Shelley against Edgar Allan Poe (1812 vs 1809 -> same decade 1810s vs 1800s -> diff 3 -> close)
        r_g1 = self.client.post(
            "/api/games/name-that-story/guess/",
            {"date": yday, "target_id": self.author_shelley.id},
            format="json",
            **headers,
        )
        self.assertEqual(r_g1.status_code, 200)
        matches = r_g1.json()["guesses"][0]["matches"]
        self.assertTrue(matches["genre"])
        self.assertFalse(matches["country"])
        self.assertEqual(matches["era"], "close")

        # Correct author guess solves and returns author card with works
        r_win = self.client.post(
            "/api/games/name-that-story/guess/",
            {"date": yday, "target_id": self.author_poe.id},
            format="json",
            **headers,
        )
        self.assertEqual(r_win.status_code, 200)
        win_data = r_win.json()
        self.assertEqual(win_data["status"], "solved")
        self.assertEqual(win_data["answer"]["kind"], "author")
        self.assertEqual(win_data["answer"]["name"], "Edgar Allan Poe")
        self.assertTrue(len(win_data["answer"]["works"]) >= 2)

        # Archive play must NOT increment streak
        st = self.client.get("/api/games/name-that-story/stats/", **headers).json()
        self.assertEqual(st["played"], 1)
        self.assertEqual(st["wins"], 1)
        self.assertEqual(st["current_streak"], 0)

    def test_guest_merge_idempotent_and_week_endpoint(self):
        guest_headers = {"HTTP_X_GUEST_KEY": "guest-merge-1"}
        self.client.post(
            "/api/games/name-that-story/guess/",
            {"target_id": self.story1.id},
            format="json",
            **guest_headers,
        )

        self.client.force_authenticate(user=self.user)
        r_m1 = self.client.post(
            "/api/games/name-that-story/merge-guest/",
            {"guest_key": "guest-merge-1"},
            format="json",
        )
        self.assertEqual(r_m1.status_code, 200)
        self.assertEqual(r_m1.json()["merged"], 1)

        # Second call is idempotent
        r_m2 = self.client.post(
            "/api/games/name-that-story/merge-guest/",
            {"guest_key": "guest-merge-1"},
            format="json",
        )
        self.assertEqual(r_m2.status_code, 200)
        self.assertEqual(r_m2.json()["merged"], 0)

        # Week endpoint returns 7 days with no clues/answers
        r_week = self.client.get("/api/games/name-that-story/week/")
        self.assertEqual(r_week.status_code, 200)
        days = r_week.json()["days"]
        self.assertEqual(len(days), 7)
        self.assertTrue(days[0]["is_today"])
        for d in days:
            self.assertNotIn("clues", d)
            self.assertNotIn("answer", d)

    def test_admin_permissions_approval_leak_block_locks_and_autofill(self):
        # Non-staff forbidden
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.get("/api/admin/games/name-that-story/puzzles/").status_code,
            403,
        )

        # Staff allowed
        self.client.force_authenticate(user=self.staff)

        # Create a trivia draft with a leaking clue
        target_date = (self.today + timedelta(days=5)).isoformat()
        r_create = self.client.post(
            "/api/admin/games/name-that-story/puzzles/",
            {
                "date": target_date,
                "puzzle_type": "trivia",
                "answer_id": self.story1.id,
                "clues": [
                    {"kind": "text", "label": "Clue 1", "value": "A famous Poe tale about a black cat."},
                    {"kind": "attribute", "key": "genre", "label": "Genre"},
                    {"kind": "attribute", "key": "country", "label": "Country"},
                    {"kind": "attribute", "key": "era", "label": "Era"},
                ],
            },
            format="json",
        )
        self.assertEqual(r_create.status_code, 201)
        pid = r_create.json()["id"]
        self.assertEqual(r_create.json()["approval_status"], "draft")

        # Approve blocked by leaking clue
        r_bad_app = self.client.post(f"/api/admin/games/name-that-story/puzzles/{pid}/approve/")
        self.assertEqual(r_bad_app.status_code, 400)
        self.assertTrue(len(r_bad_app.json()["errors"]) >= 1)

        # Fix clue and approve
        self.client.patch(
            f"/api/admin/games/name-that-story/puzzles/{pid}/",
            {
                "clues": [
                    {"kind": "text", "label": "Clue 1", "value": "A gothic horror narrator descends into guilt and madness."},
                    {"kind": "attribute", "key": "genre", "label": "Genre"},
                    {"kind": "attribute", "key": "country", "label": "Country"},
                    {"kind": "attribute", "key": "era", "label": "Era"},
                ]
            },
            format="json",
        )
        r_ok_app = self.client.post(f"/api/admin/games/name-that-story/puzzles/{pid}/approve/")
        self.assertEqual(r_ok_app.status_code, 200)
        self.assertEqual(r_ok_app.json()["approval_status"], "approved")

        # Live puzzle with attempts cannot be deleted or content-patched
        PuzzleAttempt.objects.create(puzzle=self.today_puzzle, guest_key="g-lock-1")
        self.assertEqual(
            self.client.delete(
                f"/api/admin/games/name-that-story/puzzles/{self.today_puzzle.id}/"
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/admin/games/name-that-story/puzzles/{self.today_puzzle.id}/",
                {"clues": []},
                format="json",
            ).status_code,
            409,
        )

        # Rotation PUT + Autofill idempotency
        rot_payload = {
            "rotation": [
                {"weekday": i, "puzzle_type": "opening_line" if i % 2 == 0 else "trivia"}
                for i in range(7)
            ]
        }
        r_rot = self.client.put(
            "/api/admin/games/name-that-story/rotation/",
            rot_payload,
            format="json",
        )
        self.assertEqual(r_rot.status_code, 200)
        self.assertEqual(PuzzleRotation.objects.count(), 7)

        r_af1 = self.client.post(
            "/api/admin/games/name-that-story/autofill/",
            {"days": 7},
            format="json",
        )
        self.assertEqual(r_af1.status_code, 200)
        count_after_1 = DailyPuzzle.objects.count()
        call_command("autofill_puzzles", days=7)
        self.assertEqual(DailyPuzzle.objects.count(), count_after_1)

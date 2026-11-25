import csv
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.games.models import ClueBank, MiniAttempt, MiniCrossword
from apps.games.services.engine import utc_today
from apps.story.models import Author, Story

User = get_user_model()

MOCK_PATTERN = ["....#", ".....", ".....", ".....", "#...."]
MOCK_SOLUTION = ["SPAM#", "TALES", "AGILE", "RAVEN", "#NEED"]


def make_clues(story_id=None):
    return [
        {"number": 1, "direction": "across", "answer": "SPAM", "clue": "Unwanted inbox messages", "story_id": None},
        {"number": 5, "direction": "across", "answer": "TALES", "clue": "Short stories of wonder", "story_id": None},
        {"number": 7, "direction": "across", "answer": "AGILE", "clue": "Quick and nimble", "story_id": None},
        {"number": 8, "direction": "across", "answer": "RAVEN", "clue": "Poe's midnight bird", "story_id": story_id},
        {"number": 9, "direction": "across", "answer": "NEED", "clue": "Strong requirement", "story_id": None},
        {"number": 1, "direction": "down", "answer": "STAR", "clue": "Bright night-sky point", "story_id": None},
        {"number": 2, "direction": "down", "answer": "PAGAN", "clue": "Ancient polytheistic belief", "story_id": None},
        {"number": 3, "direction": "down", "answer": "ALIVE", "clue": "Breathing and well", "story_id": None},
        {"number": 4, "direction": "down", "answer": "MELEE", "clue": "Chaotic close-quarters brawl", "story_id": None},
        {"number": 6, "direction": "down", "answer": "SEND", "clue": "Dispatch a letter", "story_id": None},
    ]


class MiniCrosswordAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.today = utc_today()

        self.user = User.objects.create_user(
            username="miniuser", email="mini@example.com", password="password123"
        )
        self.staff = User.objects.create_superuser(
            username="miniadmin", email="miniadmin@example.com", password="password123"
        )

        self.author = Author.objects.create(name="Edgar Allan Poe", slug="edgar-allan-poe")
        self.story = Story.objects.create(
            title="The Raven",
            slug="the-raven",
            author=self.author,
            is_published=True,
        )

        self.today_mini = MiniCrossword.objects.create(
            date=self.today,
            title="Poe and Gothic",
            pattern=MOCK_PATTERN,
            solution=MOCK_SOLUTION,
            clues=make_clues(self.story.id),
            status="approved",
        )
        self.yesterday_mini = MiniCrossword.objects.create(
            date=self.today - timedelta(days=1),
            title="Yesterday Mini",
            pattern=MOCK_PATTERN,
            solution=MOCK_SOLUTION,
            clues=make_clues(self.story.id),
            status="approved",
        )
        self.draft_mini = MiniCrossword.objects.create(
            date=self.today - timedelta(days=2),
            title="Draft Mini",
            pattern=MOCK_PATTERN,
            solution=MOCK_SOLUTION,
            clues=make_clues(),
            status="draft",
        )
        self.future_mini = MiniCrossword.objects.create(
            date=self.today + timedelta(days=1),
            title="Future Mini",
            pattern=MOCK_PATTERN,
            solution=MOCK_SOLUTION,
            clues=make_clues(),
            status="approved",
        )

    def test_solution_answers_and_story_links_never_sent_before_solve(self):
        r = self.client.get("/api/games/mini-crossword/today/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsNone(data["attempt"])
        self.assertNotIn("solution", data)
        self.assertNotIn("linked_stories", data)
        self.assertNotIn("share_tokens", data)
        for c in data["clues"]:
            self.assertNotIn("answer", c)
            self.assertNotIn("story_id", c)
        body = json.dumps(data)
        self.assertNotIn("SPAM", body)
        self.assertNotIn("RAVEN", body)
        self.assertNotIn("the-raven", body)

    def test_draft_and_future_return_404(self):
        d_draft = (self.today - timedelta(days=2)).isoformat()
        d_future = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(
            self.client.get(f"/api/games/mini-crossword/puzzles/{d_draft}/").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/games/mini-crossword/puzzles/{d_future}/").status_code, 404
        )

    def test_full_play_flow_check_reveal_submit_and_streak(self):
        headers = {"HTTP_X_GUEST_KEY": "guest-mini-play"}

        # Start
        r_start = self.client.post("/api/games/mini-crossword/start/", {}, format="json", **headers)
        self.assertEqual(r_start.status_code, 200)
        self.assertEqual(r_start.json()["attempt"]["status"], "in_progress")

        # Save partial with one wrong letter at (0,2)
        r_save = self.client.put(
            "/api/games/mini-crossword/save/",
            {"letters": ["SPXM#", "TALES", ".....", ".....", "#...."]},
            format="json",
            **headers,
        )
        self.assertEqual(r_save.status_code, 200)

        # Check returns wrong cells ONLY, never correct letters
        r_chk = self.client.post(
            "/api/games/mini-crossword/check/",
            {"scope": "grid"},
            format="json",
            **headers,
        )
        self.assertEqual(r_chk.status_code, 200)
        self.assertEqual(r_chk.json(), {"wrong": [[0, 2]]})

        # Reveal (0,2) twice -> hints_used counts once
        r_rev1 = self.client.post(
            "/api/games/mini-crossword/reveal/", {"row": 0, "col": 2}, format="json", **headers
        )
        self.assertEqual(r_rev1.status_code, 200)
        self.assertEqual(r_rev1.json()["letter"], "A")
        self.assertEqual(r_rev1.json()["hints_used"], 1)

        r_rev2 = self.client.post(
            "/api/games/mini-crossword/reveal/", {"row": 0, "col": 2}, format="json", **headers
        )
        self.assertEqual(r_rev2.json()["hints_used"], 1)

        # Save cannot overwrite revealed cell (0,2)
        r_over = self.client.put(
            "/api/games/mini-crossword/save/",
            {"letters": ["SPZM#", "TALES", ".....", ".....", "#...."]},
            format="json",
            **headers,
        )
        self.assertEqual(r_over.json()["letters"][0][2], "A")

        # Submit wrong grid
        r_wrong = self.client.post(
            "/api/games/mini-crossword/submit/",
            {"letters": ["SPAM#", "TALES", "AGILE", "RAVEN", "#NOOD"]},
            format="json",
            **headers,
        )
        self.assertEqual(r_wrong.status_code, 200)
        self.assertFalse(r_wrong.json()["solved"])

        # Submit right grid -> solved payload includes solution, linked_stories, share_tokens
        r_right = self.client.post(
            "/api/games/mini-crossword/submit/",
            {"letters": MOCK_SOLUTION},
            format="json",
            **headers,
        )
        self.assertEqual(r_right.status_code, 200)
        self.assertTrue(r_right.json()["solved"])
        solved_p = r_right.json()["puzzle"]
        self.assertEqual(solved_p["solution"], MOCK_SOLUTION)
        self.assertEqual(len(solved_p["linked_stories"]), 1)
        self.assertEqual(solved_p["linked_stories"][0]["slug"], "the-raven")
        self.assertEqual(solved_p["share_tokens"][0], "GGYGK")

        # Hints do NOT break streak
        st = self.client.get("/api/games/mini-crossword/stats/", **headers).json()
        self.assertEqual(st["current_streak"], 1)

        # Merge guest to user (idempotent)
        self.client.force_authenticate(user=self.user)
        m1 = self.client.post(
            "/api/games/mini-crossword/merge-guest/",
            {"guest_key": "guest-mini-play"},
            format="json",
        )
        self.assertEqual(m1.json()["merged"], 1)
        m2 = self.client.post(
            "/api/games/mini-crossword/merge-guest/",
            {"guest_key": "guest-mini-play"},
            format="json",
        )
        self.assertEqual(m2.json()["merged"], 0)

    def test_archive_play_excluded_from_streak(self):
        headers = {"HTTP_X_GUEST_KEY": "guest-mini-archive"}
        yday = (self.today - timedelta(days=1)).isoformat()
        self.client.post("/api/games/mini-crossword/start/", {"date": yday}, format="json", **headers)
        r = self.client.post(
            "/api/games/mini-crossword/submit/",
            {"date": yday, "letters": MOCK_SOLUTION},
            format="json",
            **headers,
        )
        self.assertTrue(r.json()["solved"])
        st = self.client.get("/api/games/mini-crossword/stats/", **headers).json()
        self.assertEqual(st["solved"], 1)
        self.assertEqual(st["current_streak"], 0)

    def test_admin_permissions_approval_block_autogenerate_and_clue_bank(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.get("/api/admin/games/mini-crossword/puzzles/").status_code, 403
        )

        self.client.force_authenticate(user=self.staff)
        # Create draft with a clue containing its own answer -> approve blocked
        bad_clues = make_clues()
        bad_clues[3]["clue"] = "The raven in Poe's poem"
        d_new = (self.today + timedelta(days=5)).isoformat()
        r_cr = self.client.post(
            "/api/admin/games/mini-crossword/puzzles/",
            {
                "date": d_new,
                "title": "Test",
                "pattern": MOCK_PATTERN,
                "solution": MOCK_SOLUTION,
                "clues": bad_clues,
            },
            format="json",
        )
        self.assertEqual(r_cr.status_code, 201)
        pid = r_cr.json()["id"]
        self.assertEqual(r_cr.json()["approval_status"], "draft")

        r_bad = self.client.post(f"/api/admin/games/mini-crossword/puzzles/{pid}/approve/")
        self.assertEqual(r_bad.status_code, 400)
        self.assertTrue(any("contains its own answer" in e for e in r_bad.json()["errors"]))

        # Fix and approve
        self.client.patch(
            f"/api/admin/games/mini-crossword/puzzles/{pid}/",
            {"clues": make_clues()},
            format="json",
        )
        r_ok = self.client.post(f"/api/admin/games/mini-crossword/puzzles/{pid}/approve/")
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(r_ok.json()["approval_status"], "approved")

        # CSV import into ClueBank
        csv_body = "answer,clue,story_slug\nRAVEN,Midnight visitor of Poe,the-raven\nBAD,Too short,\n"
        r_imp = self.client.post(
            "/api/admin/games/mini-crossword/clue-bank/import/",
            {"csv": csv_body},
            format="json",
        )
        self.assertEqual(r_imp.status_code, 200)
        self.assertEqual(r_imp.json()["imported"], 2)  # BAD is 3 letters so valid!
        self.assertTrue(ClueBank.objects.filter(answer="RAVEN", story=self.story).exists())

        # Autogenerate stays draft & is idempotent
        r_ag = self.client.post(
            "/api/admin/games/mini-crossword/autogenerate/", {"days": 3}, format="json"
        )
        self.assertEqual(r_ag.status_code, 200)
        count1 = MiniCrossword.objects.count()
        call_command("autogenerate_minis", days=3)
        self.assertEqual(MiniCrossword.objects.count(), count1)

    def test_difficulty_override_clue_bank_saving_and_recompute_command(self):
        self.client.force_authenticate(user=self.staff)
        d_new = (self.today + timedelta(days=9)).isoformat()

        clues = make_clues(self.story.id)
        # Mark first clue as save_to_bank=False
        clues[0]["save_to_bank"] = False

        r_cr = self.client.post(
            "/api/admin/games/mini-crossword/puzzles/",
            {
                "date": d_new,
                "title": "Bank Save Test",
                "pattern": MOCK_PATTERN,
                "solution": MOCK_SOLUTION,
                "clues": clues,
                "difficulty": "hard",
            },
            format="json",
        )
        self.assertEqual(r_cr.status_code, 201)
        data = r_cr.json()
        pid = data["id"]
        self.assertEqual(data["difficulty"], "hard")
        self.assertTrue(data["difficulty_override"])
        self.assertIsNotNone(data["difficulty_score"])

        # Reset override via difficulty="auto"
        r_reset = self.client.patch(
            f"/api/admin/games/mini-crossword/puzzles/{pid}/",
            {"difficulty": "auto"},
            format="json",
        )
        self.assertEqual(r_reset.status_code, 200)
        self.assertFalse(r_reset.json()["difficulty_override"])

        # Pre-seed one clue in ClueBank to verify increment vs create
        ClueBank.objects.create(answer="RAVEN", clue="Poe's midnight bird", times_used=2)

        # Approve (draft -> approved) creates new bank rows once & increments existing once; skips save_to_bank=False
        r_app = self.client.post(f"/api/admin/games/mini-crossword/puzzles/{pid}/approve/")
        self.assertEqual(r_app.status_code, 200)
        self.assertFalse(ClueBank.objects.filter(answer="SPAM", clue="Unwanted inbox messages").exists())
        self.assertTrue(ClueBank.objects.filter(answer="TALES", clue="Short stories of wonder", times_used=1).exists())
        self.assertEqual(ClueBank.objects.get(answer="RAVEN", clue="Poe's midnight bird").times_used, 3)

        # Re-approving an already-approved puzzle must NOT increment times_used again
        r_app2 = self.client.post(f"/api/admin/games/mini-crossword/puzzles/{pid}/approve/")
        self.assertEqual(r_app2.status_code, 200)
        self.assertEqual(ClueBank.objects.get(answer="RAVEN", clue="Poe's midnight bird").times_used, 3)

        # CSV import with is_name column
        csv_body = "answer,clue,story_slug,is_name\nOGDEN,Poet Nash,,true\nSTAR,Night light,,false\n"
        r_csv = self.client.post(
            "/api/admin/games/mini-crossword/clue-bank/import/",
            {"csv": csv_body},
            format="json",
        )
        self.assertEqual(r_csv.status_code, 200)
        self.assertTrue(ClueBank.objects.get(answer="OGDEN", clue="Poet Nash").is_name)
        self.assertFalse(ClueBank.objects.get(answer="STAR", clue="Night light").is_name)

        # recompute_mini_difficulty --dry-run changes nothing
        self.today_mini.difficulty = ""
        self.today_mini.difficulty_score = None
        self.today_mini.save(update_fields=["difficulty", "difficulty_score"])
        call_command("recompute_mini_difficulty", dry_run=True)
        self.today_mini.refresh_from_db()
        self.assertEqual(self.today_mini.difficulty, "")
        call_command("recompute_mini_difficulty")
        self.today_mini.refresh_from_db()
        self.assertIn(self.today_mini.difficulty, ("easy", "medium", "hard"))

        # Public payload includes difficulty label but never breakdown or solution
        self.client.force_authenticate(user=None)
        pub = self.client.get("/api/games/mini-crossword/today/").json()
        self.assertEqual(pub["difficulty"], self.today_mini.difficulty)
        self.assertNotIn("difficulty_breakdown", pub)
        self.assertNotIn("solution", pub)

    def test_clue_bank_cursor_pagination_and_filters(self):
        # Staff-only enforcement
        self.client.force_authenticate(user=None)
        self.assertIn(
            self.client.get("/api/admin/games/mini-crossword/clue-bank/").status_code,
            (401, 403),
        )
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.get("/api/admin/games/mini-crossword/clue-bank/").status_code,
            403,
        )

        # Seed 130 deterministic rows across 3-, 4-, and 5-letter answers
        ClueBank.objects.all().delete()
        rows_to_create = []
        for idx in range(130):
            # Mix 3, 4, and 5 letter uppercase alphabetic answers
            a = chr(ord("A") + (idx // 26) % 26)
            b = chr(ord("A") + idx % 26)
            if idx % 3 == 0:
                ans = f"A{a}{b}"
            elif idx % 3 == 1:
                ans = f"B{a}{b}X"
            else:
                ans = f"C{a}{b}XY"
            rows_to_create.append(
                ClueBank(
                    answer=ans,
                    clue=f"Clue number {idx:03d}" + (" needle" if idx % 10 == 0 else ""),
                    story=self.story if idx % 5 == 0 else None,
                    is_name=(idx % 4 == 0),
                    times_used=idx % 7,
                )
            )
        ClueBank.objects.bulk_create(rows_to_create)

        self.client.force_authenticate(user=self.staff)

        # Page 1 default size (50) + constant query count (SAVEPOINT + COUNT + SELECT + RELEASE)
        with self.assertNumQueries(4):
            r1 = self.client.get("/api/admin/games/mini-crossword/clue-bank/")
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertEqual(d1["total"], 130)
        self.assertEqual(len(d1["results"]), 50)
        self.assertIsNotNone(d1["next_cursor"])

        # Verify stable ordering (answer, clue, id)
        keys1 = [(r["answer"], r["clue"], r["id"]) for r in d1["results"]]
        self.assertEqual(keys1, sorted(keys1))

        # Insert a row BEFORE the current cursor mid-scroll; must not cause dupes or skips
        ClueBank.objects.create(answer="AAA", clue="Inserted before cursor", is_name=False)

        # Follow next_cursor to collect remaining pages with constant query count
        collected = list(d1["results"])
        cursor = d1["next_cursor"]
        while cursor:
            with self.assertNumQueries(4):
                resp = self.client.get(
                    f"/api/admin/games/mini-crossword/clue-bank/?page_size=50&cursor={cursor}"
                )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            collected.extend(payload["results"])
            cursor = payload["next_cursor"]

        # Original 130 rows seen exactly once (the mid-scroll insert at AAA was before cursor)
        ids = [r["id"] for r in collected]
        self.assertEqual(len(ids), 130)
        self.assertEqual(len(set(ids)), 130)

        # Remove helper row so count is 130 again
        ClueBank.objects.filter(answer="AAA", clue="Inserted before cursor").delete()

        # Clamp page_size > 100 to 100
        r_clamp = self.client.get("/api/admin/games/mini-crossword/clue-bank/?page_size=500")
        self.assertEqual(r_clamp.status_code, 200)
        self.assertEqual(len(r_clamp.json()["results"]), 100)
        self.assertEqual(r_clamp.json()["total"], 130)

        # Invalid cursor returns 400
        r_bad = self.client.get("/api/admin/games/mini-crossword/clue-bank/?cursor=not-valid-base64!")
        self.assertEqual(r_bad.status_code, 400)
        self.assertIn("detail", r_bad.json())

        # q + pagination
        q_collected = []
        q_cursor = None
        while True:
            url = "/api/admin/games/mini-crossword/clue-bank/?q=needle&page_size=5"
            if q_cursor:
                url += f"&cursor={q_cursor}"
            rq = self.client.get(url)
            self.assertEqual(rq.status_code, 200)
            dq = rq.json()
            self.assertEqual(dq["total"], 13)
            q_collected.extend(dq["results"])
            q_cursor = dq["next_cursor"]
            if not q_cursor:
                break
        self.assertEqual(len(q_collected), 13)
        self.assertEqual(len({r["id"] for r in q_collected}), 13)

        # length filter
        r_len3 = self.client.get("/api/admin/games/mini-crossword/clue-bank/?length=3&page_size=100")
        self.assertEqual(r_len3.status_code, 200)
        self.assertTrue(all(len(r["answer"]) == 3 for r in r_len3.json()["results"]))
        self.assertEqual(r_len3.json()["total"], ClueBank.objects.filter(answer__regex=r"^.{3}$").count())

        # story_id filter
        r_story = self.client.get(
            f"/api/admin/games/mini-crossword/clue-bank/?story_id={self.story.id}&page_size=100"
        )
        self.assertEqual(r_story.status_code, 200)
        self.assertEqual(r_story.json()["total"], 26)
        self.assertTrue(all(r["story_id"] == self.story.id for r in r_story.json()["results"]))

        # is_name filter
        r_name = self.client.get("/api/admin/games/mini-crossword/clue-bank/?is_name=true&page_size=100")
        self.assertEqual(r_name.status_code, 200)
        self.assertTrue(all(r["is_name"] is True for r in r_name.json()["results"]))

        # exact answer filter
        first_ans = d1["results"][0]["answer"]
        r_ans = self.client.get(
            f"/api/admin/games/mini-crossword/clue-bank/?answer={first_ans.lower()}"
        )
        self.assertEqual(r_ans.status_code, 200)
        self.assertTrue(all(r["answer"] == first_ans for r in r_ans.json()["results"]))

    def test_clue_bank_csv_export_roundtrip_and_command(self):
        # Staff-only
        self.client.force_authenticate(user=None)
        self.assertIn(
            self.client.get("/api/admin/games/mini-crossword/clue-bank/export/").status_code,
            (401, 403),
        )
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.get("/api/admin/games/mini-crossword/clue-bank/export/").status_code,
            403,
        )

        ClueBank.objects.all().delete()
        ClueBank.objects.create(
            answer="RAVEN",
            clue='Bird that croaked, "Nevermore"\nin Poe\'s poem',
            story=self.story,
            is_name=False,
            times_used=4,
        )
        ClueBank.objects.create(
            answer="POE",
            clue="Author of tales, macabre and eerie",
            story=None,
            is_name=True,
            times_used=2,
        )
        ClueBank.objects.create(
            answer="STAR",
            clue="Night light",
            story=None,
            is_name=False,
            times_used=1,
        )

        self.client.force_authenticate(user=self.staff)

        # Full export
        resp = self.client.get("/api/admin/games/mini-crossword/clue-bank/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])
        self.assertIn(
            f'filename="clue-bank-{utc_today().isoformat()}.csv"',
            resp["Content-Disposition"],
        )
        csv_bytes = b"".join(resp.streaming_content)
        csv_text = csv_bytes.decode("utf-8")

        parsed = list(csv.reader(io.StringIO(csv_text)))
        self.assertEqual(parsed[0], ["answer", "clue", "story_slug", "is_name", "times_used"])
        self.assertEqual(len(parsed), 4)  # header + 3 rows
        # Ordered by answer: POE, RAVEN, STAR
        self.assertEqual(parsed[1], ["POE", "Author of tales, macabre and eerie", "", "true", "2"])
        self.assertEqual(
            parsed[2],
            ["RAVEN", 'Bird that croaked, "Nevermore"\nin Poe\'s poem', "the-raven", "false", "4"],
        )
        self.assertEqual(parsed[3], ["STAR", "Night light", "", "false", "1"])

        # Filtered export (length=3)
        resp_f = self.client.get("/api/admin/games/mini-crossword/clue-bank/export/?length=3")
        self.assertEqual(resp_f.status_code, 200)
        parsed_f = list(csv.reader(io.StringIO(b"".join(resp_f.streaming_content).decode("utf-8"))))
        self.assertEqual(len(parsed_f), 2)
        self.assertEqual(parsed_f[1][0], "POE")

        # Round-trip: import exported CSV into empty bank recreates identical rows (ignoring times_used)
        ClueBank.objects.all().delete()
        r_imp = self.client.post(
            "/api/admin/games/mini-crossword/clue-bank/import/",
            {"csv": csv_text},
            format="json",
        )
        self.assertEqual(r_imp.status_code, 200)
        self.assertEqual(r_imp.json(), {"imported": 3, "updated": 0, "skipped": 0})

        reimported = list(ClueBank.objects.select_related("story").order_by("answer", "clue", "id"))
        self.assertEqual(len(reimported), 3)
        self.assertEqual(reimported[0].answer, "POE")
        self.assertTrue(reimported[0].is_name)
        self.assertIsNone(reimported[0].story)
        self.assertEqual(reimported[0].times_used, 0)  # times_used ignored on import

        self.assertEqual(reimported[1].answer, "RAVEN")
        self.assertEqual(reimported[1].clue, 'Bird that croaked, "Nevermore"\nin Poe\'s poem')
        self.assertEqual(reimported[1].story_id, self.story.id)
        self.assertFalse(reimported[1].is_name)

        # Unknown story_slug imports with story=None
        r_unk = self.client.post(
            "/api/admin/games/mini-crossword/clue-bank/import/",
            {"csv": "answer,clue,story_slug,is_name,times_used\nMOON,Night orb,nonexistent-slug,yes,99\n"},
            format="json",
        )
        self.assertEqual(r_unk.status_code, 200)
        self.assertEqual(r_unk.json()["imported"], 1)
        moon = ClueBank.objects.get(answer="MOON")
        self.assertIsNone(moon.story)
        self.assertTrue(moon.is_name)
        self.assertEqual(moon.times_used, 0)

        # Management command export_clue_bank (default omits times_used; --include-usage keeps it)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_default = Path(tmpdir) / "clues_default.csv"
            out_usage = Path(tmpdir) / "clues_usage.csv"

            stdout1 = io.StringIO()
            call_command("export_clue_bank", str(out_default), stdout=stdout1)
            self.assertIn("4", stdout1.getvalue())
            rows_def = list(csv.reader(out_default.open("r", encoding="utf-8")))
            self.assertEqual(rows_def[0], ["answer", "clue", "story_slug", "is_name"])
            self.assertEqual(len(rows_def), 5)  # header + 4 rows

            stdout2 = io.StringIO()
            call_command("export_clue_bank", str(out_usage), include_usage=True, stdout=stdout2)
            self.assertIn("4", stdout2.getvalue())
            rows_use = list(csv.reader(out_usage.open("r", encoding="utf-8")))
            self.assertEqual(rows_use[0], ["answer", "clue", "story_slug", "is_name", "times_used"])
            self.assertEqual(len(rows_use), 5)

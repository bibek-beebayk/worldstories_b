from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.stats.models import AudioReadingProgress
from apps.story.models import Audio, Audiobook, Author, Story

User = get_user_model()


class AuditAndBackfillCommandsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="audit@example.com", username="audit_user", password="password123"
        )
        self.author = Author.objects.create(name="Mary Shelley")
        self.story = Story.objects.create(
            title="Frankenstein",
            slug="frankenstein",
            about="Classic gothic novel.",
            author=self.author,
            language="en",
            is_published=True,
        )
        self.ab = Audiobook.objects.create(
            story=self.story,
            title="Frankenstein",
            slug="frankenstein",
            is_published=True,
        )
        self.track = Audio.objects.create(
            story=self.story,
            audiobook=self.ab,
            title="Chapter 1",
            slug="chapter-1",
            audio_file="story_audios/f1.mp3",
            order=1,
            duration_seconds=180.0,
        )

    def test_clean_database_passes_audit(self):
        out = StringIO()
        call_command("audit_audiobooks", stdout=out)
        self.assertIn("Audit PASSED", out.getvalue())

    def test_audit_detects_stale_cache_and_recompute_fixes_it(self):
        Audiobook.objects.filter(pk=self.ab.pk).update(track_count=99, total_duration_seconds=1.0)
        with self.assertRaises(SystemExit) as ctx:
            call_command("audit_audiobooks", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(ctx.exception.code, 1)

        # Dry run changes nothing
        out_dry = StringIO()
        call_command("recompute_audiobook_totals", "--dry-run", stdout=out_dry)
        self.assertIn("changed=1", out_dry.getvalue())
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.track_count, 99)

        # Real run fixes and is idempotent
        out_real = StringIO()
        call_command("recompute_audiobook_totals", stdout=out_real)
        self.assertIn("changed=1", out_real.getvalue())
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.track_count, 1)
        self.assertAlmostEqual(self.ab.total_duration_seconds, 180.0)

        out_second = StringIO()
        call_command("recompute_audiobook_totals", stdout=out_second)
        self.assertIn("changed=0", out_second.getvalue())

        call_command("audit_audiobooks", stdout=StringIO())

    def test_audit_detects_null_progress_and_backfill_fixes_it(self):
        prog = AudioReadingProgress.objects.create(
            user=self.user,
            story=self.story,
            audio=self.track,
            progress=0.4,
            position_seconds=72.0,
            duration_seconds=180.0,
        )
        AudioReadingProgress.objects.filter(pk=prog.pk).update(audiobook=None)

        with self.assertRaises(SystemExit) as ctx:
            call_command("audit_audiobooks", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(ctx.exception.code, 1)

        # Dry run does not write
        out_dry = StringIO()
        call_command("backfill_audio_progress", "--dry-run", stdout=out_dry)
        self.assertIn("moved=1", out_dry.getvalue())
        prog.refresh_from_db()
        self.assertIsNone(prog.audiobook_id)

        # Real run moves and is idempotent
        out_real = StringIO()
        call_command("backfill_audio_progress", stdout=out_real)
        self.assertIn("moved=1", out_real.getvalue())
        prog.refresh_from_db()
        self.assertEqual(prog.audiobook_id, self.ab.id)

        out_second = StringIO()
        call_command("backfill_audio_progress", stdout=out_second)
        self.assertIn("moved=0", out_second.getvalue())

        call_command("audit_audiobooks", stdout=StringIO())

    def test_audit_detects_mismatched_progress_and_slug_collision(self):
        other_ab = Audiobook.objects.create(
            title="Other",
            slug="other-ab",
            author=self.author,
            language="en",
            about="Other.",
            is_published=True,
        )
        prog = AudioReadingProgress.objects.create(
            user=self.user,
            audiobook=other_ab,
            audio=self.track,
            progress=0.5,
        )
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("audit_audiobooks", stdout=out, stderr=StringIO())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("6. AudioReadingProgress rows where track.audiobook != row.audiobook: 1", out.getvalue())
        prog.delete()

        # Slug collision with an unrelated published story
        Story.objects.create(
            title="Other Story",
            slug="other-ab",
            about="Collision.",
            author=self.author,
            language="en",
            is_published=True,
        )
        out2 = StringIO()
        with self.assertRaises(SystemExit) as ctx2:
            call_command("audit_audiobooks", stdout=out2, stderr=StringIO())
        self.assertEqual(ctx2.exception.code, 1)
        self.assertIn("7. Slug collisions between Audiobook and unrelated published Story: 1", out2.getvalue())

    def test_audit_warning_only_for_empty_published_audiobook(self):
        Audiobook.objects.create(
            title="Empty Published",
            slug="empty-published",
            author=self.author,
            language="en",
            about="No tracks yet.",
            is_published=True,
        )
        out = StringIO()
        call_command("audit_audiobooks", stdout=out)
        self.assertIn("2. Published audiobooks with zero tracks (warning only): 1", out.getvalue())
        self.assertIn("Audit PASSED", out.getvalue())

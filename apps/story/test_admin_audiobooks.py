from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.stats.models import AudioReadingProgress
from apps.story.models import (
    Audio,
    Audiobook,
    AudiobookSeries,
    AudioTranscriptCue,
    Author,
    Genre,
    Story,
)

User = get_user_model()


def _fake_mp3(name="01_opening_chapter.mp3", content=b"ID3" + b"\x00" * 128):
    return SimpleUploadedFile(name, content, content_type="audio/mpeg")


@override_settings(
    DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class AdminAudiobooksApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin-ab@example.com",
            username="admin_ab",
            password="password123",
        )
        self.reader = User.objects.create_user(
            email="reader-ab@example.com",
            username="reader_ab",
            password="password123",
        )
        self.author = Author.objects.create(name="Jane Austen")
        self.genre = Genre.objects.create(name="Classic", slug="classic")
        self.story = Story.objects.create(
            title="Pride and Prejudice",
            slug="pride-and-prejudice",
            about="A classic tale of manners.",
            author=self.author,
            language="en",
            is_published=True,
        )
        self.story.genres.add(self.genre)

        self.draft_story = Story.objects.create(
            title="Unpublished Draft",
            slug="unpublished-draft",
            about="Still being written.",
            author=self.author,
            language="en",
            is_published=False,
        )

    def test_non_admin_is_forbidden(self):
        # Unauthenticated
        res_anon = self.client.get("/api/admin/audiobooks/")
        self.assertIn(res_anon.status_code, (401, 403))

        # Regular authenticated user
        self.client.force_authenticate(user=self.reader)
        res_user = self.client.get("/api/admin/audiobooks/")
        self.assertEqual(res_user.status_code, 403)

        res_series = self.client.get("/api/admin/audiobook-series/")
        self.assertEqual(res_series.status_code, 403)

    def test_create_standalone_validation_and_slug_generation(self):
        self.client.force_authenticate(user=self.admin)

        # Missing required standalone fields -> field errors
        res_bad = self.client.post(
            "/api/admin/audiobooks/",
            {"title": "Midnight Tales"},
            format="json",
        )
        self.assertEqual(res_bad.status_code, 400)
        self.assertIn("author", res_bad.data)
        self.assertIn("language", res_bad.data)
        self.assertIn("about", res_bad.data)

        # Valid standalone create
        payload = {
            "title": "Midnight Tales",
            "author": self.author.id,
            "language": "en",
            "about": "Spooky stories told by firelight.",
            "narrator": "Morgan Voice",
            "genres": [self.genre.id],
        }
        res_ok = self.client.post("/api/admin/audiobooks/", payload, format="json")
        self.assertEqual(res_ok.status_code, 201)
        self.assertEqual(res_ok.data["slug"], "midnight-tales")
        self.assertTrue(res_ok.data["is_standalone"])
        self.assertEqual(res_ok.data["effective"]["author"]["id"], self.author.id)
        self.assertEqual(res_ok.data["effective"]["language"], "en")

        # Duplicate title auto-generates unique slug
        res_ok2 = self.client.post("/api/admin/audiobooks/", payload, format="json")
        self.assertEqual(res_ok2.status_code, 201)
        self.assertEqual(res_ok2.data["slug"], "midnight-tales-2")

    def test_create_linked_audiobook_falls_back_to_story(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.post(
            "/api/admin/audiobooks/",
            {"story": self.story.id},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertFalse(res.data["is_standalone"])
        self.assertEqual(res.data["title"], "Pride and Prejudice")
        self.assertIsNone(res.data["author"])
        self.assertEqual(res.data["language"], "")
        self.assertEqual(res.data["about"], "")
        self.assertEqual(res.data["effective"]["author"]["name"], "Jane Austen")
        self.assertEqual(res.data["effective"]["language"], "en")
        self.assertEqual(res.data["effective"]["about"], "A classic tale of manners.")
        self.assertEqual(len(res.data["effective"]["genres"]), 1)

    def test_list_filters_stats_and_scheduled_status(self):
        self.client.force_authenticate(user=self.admin)
        series = AudiobookSeries.objects.create(name="Classics Collection", slug="classics-collection")

        ab_linked = Audiobook.objects.create(
            story=self.story,
            title="Pride Audio",
            slug="pride-audio",
            series=series,
            is_published=True,
        )
        ab_standalone = Audiobook.objects.create(
            title="Solo Draft",
            slug="solo-draft",
            author=self.author,
            language="en",
            about="Solo about.",
            is_published=False,
        )
        ab_scheduled = Audiobook.objects.create(
            title="Future Release",
            slug="future-release",
            author=self.author,
            language="en",
            about="Coming soon.",
            is_published=True,
            publish_at=timezone.now() + timedelta(days=5),
        )
        Audio.objects.create(
            audiobook=ab_linked,
            story=self.story,
            title="Track 1",
            slug="track-1",
            audio_file="story_audios/t1.mp3",
            order=1,
            duration_seconds=120.0,
        )
        Audio.objects.create(
            audiobook=ab_standalone,
            title="Track Missing",
            slug="track-missing",
            audio_file="story_audios/t2.mp3",
            order=1,
            duration_seconds=None,
        )

        # Stats endpoint
        stats_res = self.client.get("/api/admin/audiobooks/stats/")
        self.assertEqual(stats_res.status_code, 200)
        self.assertEqual(stats_res.data["total"], 3)
        self.assertEqual(stats_res.data["standalone"], 2)
        self.assertEqual(stats_res.data["series_count"], 1)
        self.assertEqual(stats_res.data["missing_duration"], 1)

        # Filter by story_link
        res_linked = self.client.get("/api/admin/audiobooks/?story_link=linked")
        self.assertEqual([r["id"] for r in res_linked.data["results"]], [ab_linked.id])

        res_standalone = self.client.get("/api/admin/audiobooks/?story_link=standalone")
        self.assertEqual(
            {r["id"] for r in res_standalone.data["results"]},
            {ab_standalone.id, ab_scheduled.id},
        )

        # Filter by series
        res_series = self.client.get(f"/api/admin/audiobooks/?series={series.id}")
        self.assertEqual([r["id"] for r in res_series.data["results"]], [ab_linked.id])

        # Filter by status
        res_sched = self.client.get("/api/admin/audiobooks/?status=scheduled")
        self.assertEqual([r["id"] for r in res_sched.data["results"]], [ab_scheduled.id])
        self.assertEqual(res_sched.data["results"][0]["status"], "scheduled")

        res_draft = self.client.get("/api/admin/audiobooks/?status=draft")
        self.assertEqual([r["id"] for r in res_draft.data["results"]], [ab_standalone.id])
        self.assertEqual(res_draft.data["results"][0]["status"], "draft")

        res_pub = self.client.get("/api/admin/audiobooks/?status=published")
        self.assertEqual([r["id"] for r in res_pub.data["results"]], [ab_linked.id])
        self.assertEqual(res_pub.data["results"][0]["status"], "published")

        # Filter by q + ordering
        res_q = self.client.get("/api/admin/audiobooks/?q=Future")
        self.assertEqual([r["id"] for r in res_q.data["results"]], [ab_scheduled.id])

        res_order = self.client.get("/api/admin/audiobooks/?ordering=title")
        titles = [r["title"] for r in res_order.data["results"]]
        self.assertEqual(titles, sorted(titles, key=str.lower))

        # story-search returns both published and unpublished stories
        res_search = self.client.get("/api/admin/audiobooks/story-search/?q=Draft")
        self.assertEqual(res_search.status_code, 200)
        self.assertEqual([s["id"] for s in res_search.data], [self.draft_story.id])

    @patch("apps.story.reading_time.probe_audio_duration_from_bytes", return_value=185.5)
    @patch("apps.story.audio_processing.normalize_uploaded_audio", side_effect=lambda f: f)
    def test_multi_file_upload_creates_tracks_and_reports_bad_files(self, _mock_norm, _mock_probe):
        self.client.force_authenticate(user=self.admin)
        ab = Audiobook.objects.create(
            title="Upload Book",
            slug="upload-book",
            author=self.author,
            language="en",
            about="About upload book.",
        )

        good1 = _fake_mp3("01_the_beginning.mp3")
        bad = SimpleUploadedFile("notes.txt", b"not audio", content_type="text/plain")
        good2 = _fake_mp3("02-second-chapter.mp3")

        res = self.client.post(
            f"/api/admin/audiobooks/{ab.id}/tracks/",
            {"files": [good1, bad, good2]},
            format="multipart",
        )
        self.assertEqual(res.status_code, 207)
        self.assertEqual(len(res.data["created"]), 2)
        self.assertEqual(len(res.data["errors"]), 1)
        self.assertEqual(res.data["errors"][0]["filename"], "notes.txt")
        self.assertEqual(res.data["created"][0]["title"], "01 The Beginning")
        self.assertEqual(res.data["created"][0]["order"], 1)
        self.assertEqual(res.data["created"][1]["title"], "02 Second Chapter")
        self.assertEqual(res.data["created"][1]["order"], 2)

        ab.refresh_from_db()
        self.assertEqual(ab.track_count, 2)
        self.assertAlmostEqual(ab.total_duration_seconds, 371.0)

    def test_reorder_tracks_and_reject_foreign_ids(self):
        self.client.force_authenticate(user=self.admin)
        ab1 = Audiobook.objects.create(
            title="Book 1", slug="book-1", author=self.author, language="en", about="About 1"
        )
        ab2 = Audiobook.objects.create(
            title="Book 2", slug="book-2", author=self.author, language="en", about="About 2"
        )
        t1 = Audio.objects.create(
            audiobook=ab1, title="One", slug="one", audio_file="story_audios/1.mp3", order=1, duration_seconds=60
        )
        t2 = Audio.objects.create(
            audiobook=ab1, title="Two", slug="two", audio_file="story_audios/2.mp3", order=2, duration_seconds=60
        )
        t_foreign = Audio.objects.create(
            audiobook=ab2, title="Foreign", slug="f", audio_file="story_audios/f.mp3", order=1, duration_seconds=60
        )

        # Reject foreign id
        res_bad = self.client.post(
            f"/api/admin/audiobooks/{ab1.id}/tracks/reorder/",
            {"ids": [t1.id, t_foreign.id]},
            format="json",
        )
        self.assertEqual(res_bad.status_code, 400)

        # Valid reorder
        res_ok = self.client.post(
            f"/api/admin/audiobooks/{ab1.id}/tracks/reorder/",
            {"ids": [t2.id, t1.id]},
            format="json",
        )
        self.assertEqual(res_ok.status_code, 200)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t2.order, 1)
        self.assertEqual(t1.order, 2)

    def test_move_track_refreshes_both_caches_and_keeps_progress(self):
        self.client.force_authenticate(user=self.admin)
        source = Audiobook.objects.create(
            title="Source", slug="source", author=self.author, language="en", about="Source about"
        )
        target = Audiobook.objects.create(
            story=self.story, title="Target", slug="target"
        )
        t1 = Audio.objects.create(
            audiobook=source,
            title="Move Me",
            slug="move-me",
            audio_file="story_audios/m1.mp3",
            transcript="<p>Hello world</p>",
            order=1,
            duration_seconds=100.0,
        )
        t2 = Audio.objects.create(
            audiobook=source,
            title="Stay Here",
            slug="stay-here",
            audio_file="story_audios/m2.mp3",
            order=2,
            duration_seconds=200.0,
        )
        cue = AudioTranscriptCue.objects.create(audio=t1, order=1, start_ms=0, end_ms=1000, text="Hello world")
        progress = AudioReadingProgress.objects.create(
            user=self.reader, audio=t1, audiobook=source, progress=0.5, position_seconds=50.0, duration_seconds=100.0
        )

        res = self.client.post(
            f"/api/admin/audiobooks/{source.id}/tracks/{t1.id}/move/",
            {"audiobook_id": target.id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)

        t1.refresh_from_db()
        t2.refresh_from_db()
        source.refresh_from_db()
        target.refresh_from_db()

        self.assertEqual(t1.audiobook_id, target.id)
        self.assertEqual(t1.story_id, self.story.id)
        self.assertEqual(t1.order, 1)
        self.assertEqual(t1.transcript, "<p>Hello world</p>")
        self.assertTrue(AudioTranscriptCue.objects.filter(pk=cue.pk, audio=t1).exists())
        self.assertTrue(AudioReadingProgress.objects.filter(pk=progress.pk, audio=t1).exists())
        # Gap closed on source
        self.assertEqual(t2.order, 1)
        # Both caches refreshed
        self.assertEqual(source.track_count, 1)
        self.assertAlmostEqual(source.total_duration_seconds, 200.0)
        self.assertEqual(target.track_count, 1)
        self.assertAlmostEqual(target.total_duration_seconds, 100.0)

    def test_delete_track_closes_gap_and_refreshes_cache(self):
        self.client.force_authenticate(user=self.admin)
        ab = Audiobook.objects.create(
            title="Gap Book", slug="gap-book", author=self.author, language="en", about="Gap"
        )
        t1 = Audio.objects.create(
            audiobook=ab, title="T1", slug="t1", audio_file="story_audios/g1.mp3", order=1, duration_seconds=50.0
        )
        t2 = Audio.objects.create(
            audiobook=ab, title="T2", slug="t2", audio_file="story_audios/g2.mp3", order=2, duration_seconds=75.0
        )

        res = self.client.delete(f"/api/admin/audiobooks/{ab.id}/tracks/{t1.id}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Audio.objects.filter(pk=t1.pk).exists())
        t2.refresh_from_db()
        self.assertEqual(t2.order, 1)
        ab.refresh_from_db()
        self.assertEqual(ab.track_count, 1)
        self.assertAlmostEqual(ab.total_duration_seconds, 75.0)

    def test_delete_audiobook_cascades_tracks_and_keeps_story(self):
        self.client.force_authenticate(user=self.admin)
        ab = Audiobook.objects.create(story=self.story, title="Linked Delete", slug="linked-delete")
        t1 = Audio.objects.create(
            audiobook=ab,
            story=self.story,
            title="Track",
            slug="track",
            audio_file="story_audios/d1.mp3",
            transcript="<p>Text</p>",
            order=1,
            duration_seconds=90.0,
        )
        AudioTranscriptCue.objects.create(audio=t1, order=1, start_ms=0, end_ms=500, text="Text")
        AudioReadingProgress.objects.create(
            user=self.reader, story=self.story, audio=t1, audiobook=ab, progress=0.25
        )

        res = self.client.delete(f"/api/admin/audiobooks/{ab.id}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Audiobook.objects.filter(pk=ab.pk).exists())
        self.assertFalse(Audio.objects.filter(pk=t1.pk).exists())
        self.assertEqual(AudioTranscriptCue.objects.filter(audio_id=t1.pk).count(), 0)
        self.assertEqual(AudioReadingProgress.objects.filter(audio_id=t1.pk).count(), 0)
        # Linked story remains intact
        self.assertTrue(Story.objects.filter(pk=self.story.pk).exists())

    def test_delete_series_nulls_audiobook_series(self):
        self.client.force_authenticate(user=self.admin)
        res_create = self.client.post(
            "/api/admin/audiobook-series/",
            {"title": "Bedtime Stories", "description": "Calm tales."},
            format="json",
        )
        self.assertEqual(res_create.status_code, 201)
        self.assertEqual(res_create.data["slug"], "bedtime-stories")
        series_id = res_create.data["id"]

        ab = Audiobook.objects.create(
            title="Lullaby",
            slug="lullaby",
            author=self.author,
            language="en",
            about="Soft.",
            series_id=series_id,
        )

        res_del = self.client.delete(f"/api/admin/audiobook-series/{series_id}/")
        self.assertEqual(res_del.status_code, 204)
        ab.refresh_from_db()
        self.assertIsNone(ab.series_id)
        self.assertTrue(Audiobook.objects.filter(pk=ab.pk).exists())

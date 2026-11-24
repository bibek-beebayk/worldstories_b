from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APITestCase

from apps.stats.models import AudioReadingProgress
from apps.story.models import Audio, Audiobook, Author, Story

User = get_user_model()


class AudiobookProgressApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="listener-p3@example.com",
            username="listener_p3",
            password="password123",
        )
        self.author = Author.objects.create(name="Arthur Conan Doyle")
        self.story = Story.objects.create(
            title="A Study in Scarlet",
            slug="study-in-scarlet",
            about="First Sherlock Holmes novel.",
            author=self.author,
            language="en",
            is_published=True,
        )
        self.linked_ab = Audiobook.objects.create(
            story=self.story,
            title="A Study in Scarlet",
            slug="study-in-scarlet",
            is_published=True,
        )
        self.linked_track1 = Audio.objects.create(
            story=self.story,
            audiobook=self.linked_ab,
            title="Chapter 1",
            slug="chapter-1",
            audio_file="story_audios/s1.mp3",
            order=1,
            duration_seconds=300.0,
        )
        self.linked_track2 = Audio.objects.create(
            story=self.story,
            audiobook=self.linked_ab,
            title="Chapter 2",
            slug="chapter-2",
            audio_file="story_audios/s2.mp3",
            order=2,
            duration_seconds=300.0,
        )
        self.standalone_ab = Audiobook.objects.create(
            title="Midnight Solo",
            slug="midnight-solo",
            author=self.author,
            language="en",
            about="A standalone audiobook.",
            is_published=True,
        )
        self.standalone_track = Audio.objects.create(
            audiobook=self.standalone_ab,
            title="Part 1",
            slug="part-1",
            audio_file="story_audios/solo1.mp3",
            order=1,
            duration_seconds=240.0,
        )

    def test_anonymous_is_rejected(self):
        res_get = self.client.get(f"/api/audio-progress/audiobook/{self.linked_ab.slug}/")
        self.assertIn(res_get.status_code, (401, 403))

        res_put = self.client.put(
            f"/api/audio-progress/audiobook/{self.linked_ab.slug}/",
            {"audio_id": self.linked_track1.id, "progress": 0.4, "position_seconds": 120},
            format="json",
        )
        self.assertIn(res_put.status_code, (401, 403))

    def test_get_and_put_audiobook_progress(self):
        self.client.force_authenticate(user=self.user)

        res_missing = self.client.get(f"/api/audio-progress/audiobook/{self.linked_ab.slug}/")
        self.assertEqual(res_missing.status_code, 404)

        res_put = self.client.put(
            f"/api/audio-progress/audiobook/{self.linked_ab.slug}/",
            {
                "audio_id": self.linked_track1.id,
                "progress": 0.5,
                "position_seconds": 150.0,
                "duration_seconds": 300.0,
            },
            format="json",
        )
        self.assertEqual(res_put.status_code, 200)
        self.assertEqual(res_put.data["audio_id"], self.linked_track1.id)
        self.assertEqual(res_put.data["audio_slug"], "chapter-1")
        self.assertAlmostEqual(res_put.data["position_seconds"], 150.0)
        self.assertAlmostEqual(res_put.data["overall_progress"], 0.25)

        row = AudioReadingProgress.objects.get(user=self.user, audio=self.linked_track1)
        self.assertEqual(row.audiobook_id, self.linked_ab.id)

        res_get = self.client.get(f"/api/audio-progress/audiobook/{self.linked_ab.slug}/")
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.data["audio_id"], self.linked_track1.id)
        self.assertAlmostEqual(res_get.data["position_seconds"], 150.0)

    def test_audiobook_progress_reads_audiobook_key_only_and_upgrades_on_put(self):
        self.client.force_authenticate(user=self.user)

        # Create a legacy row with audiobook=None (bypass signal by updating directly)
        legacy = AudioReadingProgress.objects.create(
            user=self.user,
            story=self.story,
            audio=self.linked_track1,
            progress=0.35,
            position_seconds=105.0,
            duration_seconds=300.0,
        )
        AudioReadingProgress.objects.filter(pk=legacy.pk).update(audiobook=None)

        # Phase 4 Step 2: GET reads audiobook-keyed rows only (no legacy fallback)
        res_get = self.client.get(f"/api/audio-progress/audiobook/{self.linked_ab.slug}/")
        self.assertEqual(res_get.status_code, 404)

        # PUT writes audiobook-keyed row
        res_put = self.client.put(
            f"/api/audio-progress/audiobook/{self.linked_ab.slug}/",
            {
                "audio_slug": "chapter-1",
                "progress": 0.6,
                "position_seconds": 180.0,
                "duration_seconds": 300.0,
            },
            format="json",
        )
        self.assertEqual(res_put.status_code, 200)
        legacy.refresh_from_db()
        self.assertEqual(legacy.audiobook_id, self.linked_ab.id)
        self.assertAlmostEqual(legacy.position_seconds, 180.0)

    def test_standalone_audiobook_saves_and_reloads(self):
        self.client.force_authenticate(user=self.user)

        res_put = self.client.put(
            f"/api/audio-progress/audiobook/{self.standalone_ab.slug}/",
            {
                "audio_id": self.standalone_track.id,
                "progress": 0.4,
                "position_seconds": 96.0,
                "duration_seconds": 240.0,
            },
            format="json",
        )
        self.assertEqual(res_put.status_code, 200)
        self.assertEqual(res_put.data["audio_slug"], "part-1")
        self.assertAlmostEqual(res_put.data["position_seconds"], 96.0)

        row = AudioReadingProgress.objects.get(user=self.user, audio=self.standalone_track)
        self.assertIsNone(row.story_id)
        self.assertEqual(row.audiobook_id, self.standalone_ab.id)

        res_get = self.client.get(f"/api/audio-progress/audiobook/{self.standalone_ab.slug}/")
        self.assertEqual(res_get.status_code, 200)
        self.assertAlmostEqual(res_get.data["position_seconds"], 96.0)

    def test_overview_continue_listening_merges_and_deduplicates(self):
        self.client.force_authenticate(user=self.user)

        # Legacy row for linked_track1 (audiobook=None)
        p_legacy = AudioReadingProgress.objects.create(
            user=self.user,
            story=self.story,
            audio=self.linked_track1,
            progress=0.2,
            position_seconds=60.0,
            duration_seconds=300.0,
        )
        AudioReadingProgress.objects.filter(pk=p_legacy.pk).update(
            audiobook=None,
            updated_at=timezone.now() - timedelta(minutes=10),
        )
        # Audiobook-keyed row for linked_track2 (should win over legacy for same audiobook!)
        AudioReadingProgress.objects.create(
            user=self.user,
            story=self.story,
            audiobook=self.linked_ab,
            audio=self.linked_track2,
            progress=0.7,
            position_seconds=210.0,
            duration_seconds=300.0,
        )
        # Standalone audiobook progress
        AudioReadingProgress.objects.create(
            user=self.user,
            audiobook=self.standalone_ab,
            audio=self.standalone_track,
            progress=0.5,
            position_seconds=120.0,
            duration_seconds=240.0,
        )

        res = self.client.get("/api/audiobooks/overview/")
        self.assertEqual(res.status_code, 200)
        items = res.data["continue_listening"]
        slugs = [item["audiobook"]["slug"] for item in items]
        # Exactly one entry per audiobook, no duplicates
        self.assertEqual(len(slugs), 2)
        self.assertEqual(set(slugs), {self.linked_ab.slug, self.standalone_ab.slug})
        linked_entry = next(i for i in items if i["audiobook"]["slug"] == self.linked_ab.slug)
        # Audiobook-keyed row won over legacy row
        self.assertEqual(linked_entry["track_slug"], "chapter-2")
        self.assertEqual(linked_entry["progress_percent"], 70)

    def test_old_story_progress_endpoint_delegates_and_logs_deprecation(self):
        self.client.force_authenticate(user=self.user)
        with self.assertLogs("apps.stats.views", level="WARNING") as cm:
            res_put = self.client.put(
                f"/api/audio-progress/{self.story.slug}/",
                {
                    "audio_slug": "chapter-1",
                    "progress": 0.25,
                    "position_seconds": 75.0,
                    "duration_seconds": 300.0,
                },
                format="json",
            )
        self.assertEqual(res_put.status_code, 200)
        self.assertEqual(res_put.data["audio_slug"], "chapter-1")
        self.assertTrue(any("Deprecated" in msg for msg in cm.output))

        # Delegated write stored audiobook key without requiring story write
        row = AudioReadingProgress.objects.get(user=self.user, audio=self.linked_track1)
        self.assertEqual(row.audiobook_id, self.linked_ab.id)

        with self.assertLogs("apps.stats.views", level="WARNING"):
            res_get = self.client.get(f"/api/audio-progress/{self.story.slug}/")
        self.assertEqual(res_get.status_code, 200)
        self.assertAlmostEqual(res_get.data["position_seconds"], 75.0)

    def test_sitemap_includes_published_audiobooks_once_and_story_exposes_audiobooks(self):
        res_sitemap = self.client.get("/api/sitemap.xml")
        self.assertEqual(res_sitemap.status_code, 200)
        body = res_sitemap.content.decode("utf-8")
        self.assertIn(f"/listen/{self.linked_ab.slug}</loc>", body)
        self.assertIn(f"/listen/{self.standalone_ab.slug}</loc>", body)
        self.assertEqual(body.count(f"/listen/{self.linked_ab.slug}</loc>"), 1)

        res_story = self.client.get(f"/api/stories/{self.story.slug}/")
        self.assertEqual(res_story.status_code, 200)
        self.assertEqual(len(res_story.data["audiobooks"]), 1)
        self.assertEqual(res_story.data["audiobooks"][0]["slug"], self.linked_ab.slug)

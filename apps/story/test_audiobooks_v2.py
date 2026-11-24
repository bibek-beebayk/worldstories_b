from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from apps.stats.models import AudioReadingProgress
from apps.story.models import (
    Audio,
    Audiobook,
    AudiobookSeries,
    AudiobookSeriesItem,
    Author,
    Genre,
    Story,
)
from apps.users.models import User


class AudiobookSignalsAndCacheTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="ab-sig@example.com", username="absig", password="password123"
        )
        self.story = Story.objects.create(
            title="Signal Tale", slug="signal-tale", is_published=True
        )

    def test_saving_and_deleting_audio_refreshes_audiobook_cache(self):
        ab = Audiobook.objects.create(
            story=self.story, title=self.story.title, slug="signal-tale"
        )
        a1 = Audio.objects.create(
            story=self.story,
            audiobook=ab,
            title="Ch 1",
            slug="sig-ch-1",
            order=1,
            audio_file="story_audios/sig1.mp3",
            duration_seconds=300.0,
        )
        ab.refresh_from_db()
        self.assertEqual(ab.track_count, 1)
        self.assertEqual(ab.total_duration_seconds, 300.0)

        a2 = Audio.objects.create(
            story=self.story,
            audiobook=ab,
            title="Ch 2",
            slug="sig-ch-2",
            order=2,
            audio_file="story_audios/sig2.mp3",
            duration_seconds=450.0,
        )
        ab.refresh_from_db()
        self.assertEqual(ab.track_count, 2)
        self.assertEqual(ab.total_duration_seconds, 750.0)

        # Adding a track with unknown duration makes total_duration_seconds None
        a3 = Audio.objects.create(
            story=self.story,
            audiobook=ab,
            title="Ch 3",
            slug="sig-ch-3",
            order=3,
            audio_file="story_audios/sig3.mp3",
            duration_seconds=None,
        )
        ab.refresh_from_db()
        self.assertEqual(ab.track_count, 3)
        self.assertIsNone(ab.total_duration_seconds)

        # Deleting the unknown-duration track restores the sum
        a3.delete()
        ab.refresh_from_db()
        self.assertEqual(ab.track_count, 2)
        self.assertEqual(ab.total_duration_seconds, 750.0)
        self.assertEqual(a1.audiobook_id, ab.id)
        self.assertEqual(a2.audiobook_id, ab.id)


class BackfillAudiobooksCommandTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="bf@example.com", username="bfuser", password="password123"
        )
        self.story_with_audio = Story.objects.create(
            title="Backfill Story", slug="backfill-story", is_published=True
        )
        self.story_no_audio = Story.objects.create(
            title="Text Only", slug="text-only", is_published=True
        )
        # Create raw-like unlinked audio + progress by clearing audiobook after creation
        self.audio = Audio.objects.create(
            story=self.story_with_audio,
            title="Part 1",
            slug="bf-p1",
            order=1,
            audio_file="story_audios/bf1.mp3",
            duration_seconds=600.0,
        )
        self.progress = AudioReadingProgress.objects.create(
            user=self.user, story=self.story_with_audio, audio=self.audio, progress=0.3
        )
        # Simulate pre-backfill legacy state
        Audio.objects.filter(pk=self.audio.pk).update(audiobook=None)
        AudioReadingProgress.objects.filter(pk=self.progress.pk).update(audiobook=None)
        Audiobook.objects.filter(story=self.story_with_audio).delete()

    def test_dry_run_writes_nothing_and_real_run_is_idempotent(self):
        dry_out = StringIO()
        call_command("backfill_audiobooks", "--dry-run", stdout=dry_out)
        self.assertEqual(Audiobook.objects.count(), 0)
        self.assertIn("Audiobooks created: 1", dry_out.getvalue())

        out1 = StringIO()
        call_command("backfill_audiobooks", stdout=out1)
        self.assertEqual(Audiobook.objects.count(), 1)
        self.assertFalse(Audiobook.objects.filter(story=self.story_no_audio).exists())

        ab = Audiobook.objects.get(story=self.story_with_audio)
        self.assertEqual(ab.slug, "backfill-story")
        self.assertEqual(ab.title, "")
        self.assertEqual(ab.effective_title(), "Backfill Story")
        self.assertEqual(ab.track_count, 1)
        self.assertEqual(ab.total_duration_seconds, 600.0)

        self.audio.refresh_from_db()
        self.progress.refresh_from_db()
        self.assertEqual(self.audio.audiobook_id, ab.id)
        self.assertEqual(self.progress.audiobook_id, ab.id)

        # Second run is a no-op
        out2 = StringIO()
        call_command("backfill_audiobooks", stdout=out2)
        self.assertIn("Audiobooks created: 0, tracks attached: 0, progress rows updated: 0", out2.getvalue())

    def test_slug_collision_handling(self):
        # Occupy the story's slug with an unrelated standalone audiobook
        Audiobook.objects.create(title="Taken", slug="backfill-story")
        out = StringIO()
        call_command("backfill_audiobooks", stdout=out)
        ab = Audiobook.objects.get(story=self.story_with_audio)
        self.assertEqual(ab.slug, "backfill-story-audiobook")


class PublicAudiobookApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="pub@example.com", username="pubuser", password="password123"
        )
        self.author = Author.objects.create(name="Mary Shelley", slug="mary-shelley")
        self.genre_scifi = Genre.objects.create(name="Sci-Fi", slug="sci-fi")
        self.genre_mystery = Genre.objects.create(name="Mystery", slug="mystery")
        self.series = AudiobookSeries.objects.create(
            name="Classics", slug="classics", description="Classic listens", order=1, is_active=True
        )

        # Story-linked audiobook (inherits title/author/genre/language/cover)
        self.story = Story.objects.create(
            title="Frankenstein",
            slug="frankenstein",
            author=self.author,
            language="en",
            views=500,
            rating=4.8,
            cover_image="https://example.com/frank.jpg",
            is_published=True,
        )
        self.story.genres.add(self.genre_scifi)
        self.ab_story = Audiobook.objects.create(
            story=self.story,
            title="",
            slug="frankenstein",
            narrator="Jane Narrator",
            is_published=True,
        )
        AudiobookSeriesItem.objects.create(series=self.series, audiobook=self.ab_story, order=1)
        self.t1 = Audio.objects.create(
            story=self.story,
            audiobook=self.ab_story,
            title="Letter 1",
            slug="frank-1",
            order=1,
            audio_file="story_audios/f1.mp3",
            duration_seconds=900.0,
            transcript="<p>Hello world</p>",
        )

        # Second version of the same story (for other_versions)
        self.ab_version2 = Audiobook.objects.create(
            story=self.story,
            title="Frankenstein (Full Cast)",
            slug="frankenstein-cast",
            narrator="Full Cast",
            is_published=True,
        )
        Audio.objects.create(
            audiobook=self.ab_version2,
            title="Part 1",
            slug="cast-1",
            order=1,
            audio_file="story_audios/c1.mp3",
            duration_seconds=4000.0,
        )

        # Standalone audiobook
        self.ab_standalone = Audiobook.objects.create(
            title="Midnight Mystery",
            slug="midnight-mystery",
            narrator="Bob Voice",
            language="fr",
            is_published=True,
        )
        AudiobookSeriesItem.objects.create(
            series=self.series, audiobook=self.ab_standalone, order=2
        )
        self.ab_standalone.genres.add(self.genre_mystery)
        Audio.objects.create(
            audiobook=self.ab_standalone,
            title="Episode 1",
            slug="mid-1",
            order=1,
            audio_file="story_audios/m1.mp3",
            duration_seconds=9000.0,
        )

        # Unpublished audiobook (must not appear)
        self.ab_draft = Audiobook.objects.create(
            title="Hidden Draft", slug="hidden-draft", is_published=False
        )
        Audio.objects.create(
            audiobook=self.ab_draft,
            title="Draft 1",
            slug="d-1",
            order=1,
            audio_file="story_audios/d1.mp3",
            duration_seconds=500.0,
        )

    def test_list_filters_sorts_and_pagination(self):
        resp = self.client.get("/api/audiobooks/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pagination", resp.data)
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(set(slugs), {"frankenstein", "frankenstein-cast", "midnight-mystery"})
        self.assertNotIn("hidden-draft", slugs)

        # Genre filter matches inherited story genres and own genres
        scifi = self.client.get("/api/audiobooks/?genre=sci-fi")
        self.assertEqual({r["slug"] for r in scifi.data["results"]}, {"frankenstein", "frankenstein-cast"})
        mystery = self.client.get(f"/api/audiobooks/?genre={self.genre_mystery.id}")
        self.assertEqual([r["slug"] for r in mystery.data["results"]], ["midnight-mystery"])

        # Language filter (effective)
        fr = self.client.get("/api/audiobooks/?language=fr")
        self.assertEqual([r["slug"] for r in fr.data["results"]], ["midnight-mystery"])

        # Read-along filter
        ra = self.client.get("/api/audiobooks/?has_read_along=true")
        self.assertEqual([r["slug"] for r in ra.data["results"]], ["frankenstein"])

        # Audio length buckets
        short = self.client.get("/api/audiobooks/?audio_length=short")
        self.assertEqual([r["slug"] for r in short.data["results"]], ["frankenstein"])
        medium = self.client.get("/api/audiobooks/?audio_length=medium")
        self.assertEqual([r["slug"] for r in medium.data["results"]], ["frankenstein-cast"])
        long_res = self.client.get("/api/audiobooks/?audio_length=long")
        self.assertEqual([r["slug"] for r in long_res.data["results"]], ["midnight-mystery"])

        # Sort shortest / longest / popular
        shortest = self.client.get("/api/audiobooks/?sort=shortest")
        self.assertEqual(
            [r["slug"] for r in shortest.data["results"]],
            ["frankenstein", "frankenstein-cast", "midnight-mystery"],
        )
        popular = self.client.get("/api/audiobooks/?sort=popular")
        # Linked stories before standalone
        self.assertEqual(popular.data["results"][-1]["slug"], "midnight-mystery")

    def test_detail_tracks_other_versions_and_overview(self):
        detail = self.client.get("/api/audiobooks/frankenstein/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["author"], "Mary Shelley")
        self.assertEqual(detail.data["minutes"], 15)
        self.assertTrue(detail.data["has_read_along"])
        self.assertEqual(len(detail.data["tracks"]), 1)
        self.assertEqual([v["slug"] for v in detail.data["other_versions"]], ["frankenstein-cast"])

        # Overview (anonymous)
        ov = self.client.get("/api/audiobooks/overview/")
        self.assertEqual(ov.status_code, 200)
        self.assertEqual(ov.data["count"], 3)
        self.assertIsNotNone(ov.data["featured"])
        self.assertEqual(len(ov.data["shelves"]), 1)
        self.assertEqual(ov.data["continue_listening"], [])

        # Overview (authenticated with progress)
        AudioReadingProgress.objects.create(
            user=self.user, audiobook=self.ab_story, audio=self.t1, progress=0.4
        )
        self.client.force_authenticate(user=self.user)
        ov_auth = self.client.get("/api/audiobooks/overview/")
        self.assertEqual(ov_auth.status_code, 200)
        self.assertEqual(len(ov_auth.data["continue_listening"]), 1)
        self.assertEqual(ov_auth.data["continue_listening"][0]["audiobook"]["slug"], "frankenstein")

    def test_list_query_count_is_constant(self):
        with CaptureQueriesContext(connection) as before:
            resp1 = self.client.get("/api/audiobooks/")
        self.assertEqual(resp1.status_code, 200)

        for i in range(6):
            ab = Audiobook.objects.create(title=f"Extra {i}", slug=f"extra-ab-{i}", is_published=True)
            Audio.objects.create(
                audiobook=ab,
                title="Track 1",
                slug=f"extra-tr-{i}",
                order=1,
                audio_file=f"story_audios/e{i}.mp3",
                duration_seconds=1200.0,
            )

        with CaptureQueriesContext(connection) as after:
            resp2 = self.client.get("/api/audiobooks/")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(len(after), len(before))

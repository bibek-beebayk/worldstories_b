from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.stats.completion import completed_source
from apps.stats.models import AudioReadingProgress, StoryCompletion
from apps.story.models import Audio, Audiobook, Author, Story

User = get_user_model()


class AudiobookAnalyticsPhase4Tests(APITestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(
            email="u1-p4@example.com", username="u1_p4", password="password123"
        )
        self.u2 = User.objects.create_user(
            email="u2-p4@example.com", username="u2_p4", password="password123"
        )
        self.author = Author.objects.create(name="Edgar Allan Poe")
        self.story = Story.objects.create(
            title="The Raven",
            slug="the-raven",
            about="Classic poem.",
            author=self.author,
            language="en",
            is_published=True,
            views=10,
        )
        self.linked_ab = Audiobook.objects.create(
            story=self.story, title="The Raven", slug="the-raven", is_published=True
        )
        self.linked_track = Audio.objects.create(
            story=self.story,
            audiobook=self.linked_ab,
            title="Full Reading",
            slug="full-reading",
            audio_file="story_audios/raven.mp3",
            order=1,
            duration_seconds=300.0,
        )
        self.standalone_ab = Audiobook.objects.create(
            title="Standalone Hit",
            slug="standalone-hit",
            author=self.author,
            language="en",
            about="Popular standalone audiobook.",
            is_published=True,
        )
        self.standalone_track = Audio.objects.create(
            audiobook=self.standalone_ab,
            title="Part 1",
            slug="part-1",
            audio_file="story_audios/hit.mp3",
            order=1,
            duration_seconds=400.0,
        )

    def test_standalone_audiobook_listens_appear_in_popular_sort(self):
        # Two distinct listeners on standalone audiobook, zero on linked audiobook
        AudioReadingProgress.objects.create(
            user=self.u1, audiobook=self.standalone_ab, audio=self.standalone_track, progress=0.5
        )
        AudioReadingProgress.objects.create(
            user=self.u2, audiobook=self.standalone_ab, audio=self.standalone_track, progress=0.8
        )

        res = self.client.get("/api/audiobooks/?sort=popular")
        self.assertEqual(res.status_code, 200)
        slugs = [item["slug"] for item in res.data["results"]]
        self.assertEqual(slugs[0], "standalone-hit")

    def test_story_completion_uses_audiobook_key_without_legacy_story_column(self):
        # Write progress with audiobook set and story=None (Phase 4 Step 2 behavior)
        AudioReadingProgress.objects.create(
            user=self.u1,
            story=None,
            audiobook=self.linked_ab,
            audio=self.linked_track,
            progress=1.0,
        )
        self.assertEqual(completed_source(self.u1, self.story), StoryCompletion.SOURCE_AUDIO)

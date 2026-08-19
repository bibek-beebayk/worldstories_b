from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.stats.models import (
    AudioReadingProgress,
    ChapterReadingProgress,
    FileReadingProgress,
    ReadingProgress,
)
from apps.story.models import Audio, Chapter, Genre, Story


User = get_user_model()


class TokenLifecycleApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="reader@example.com",
            username="reader",
            password="test-password",
        )

    def test_refresh_rotates_and_blacklists_the_old_token(self):
        original_refresh = str(RefreshToken.for_user(self.user))

        first_response = self.client.post(reverse("token_refresh"), {"refresh": original_refresh})
        self.assertEqual(first_response.status_code, 200)
        rotated_refresh = first_response.data["refresh"]
        self.assertNotEqual(rotated_refresh, original_refresh)

        # The original token was single-use — replaying it must fail now,
        # even though its own lifetime hasn't expired.
        replay_response = self.client.post(reverse("token_refresh"), {"refresh": original_refresh})
        self.assertEqual(replay_response.status_code, 401)

        # The rotated token it handed back is the one that's actually live.
        second_response = self.client.post(reverse("token_refresh"), {"refresh": rotated_refresh})
        self.assertEqual(second_response.status_code, 200)

    def test_logout_blacklists_the_refresh_token(self):
        refresh_token = str(RefreshToken.for_user(self.user))
        self.client.force_authenticate(self.user)

        response = self.client.post(reverse("auth-logout"), {"refresh": refresh_token})

        self.assertEqual(response.status_code, 200)
        replay_response = self.client.post(reverse("token_refresh"), {"refresh": refresh_token})
        self.assertEqual(replay_response.status_code, 401)

    def test_logout_requires_authentication(self):
        response = self.client.post(reverse("auth-logout"), {"refresh": "irrelevant"})

        self.assertEqual(response.status_code, 401)

    def test_logout_without_a_refresh_token_still_succeeds(self):
        self.client.force_authenticate(self.user)

        response = self.client.post(reverse("auth-logout"), {})

        self.assertEqual(response.status_code, 200)


class ProfileInsightsApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="reader@example.com",
            username="reader",
            password="test-password",
        )

    def test_profile_insights_requires_authentication(self):
        response = self.client.get(reverse("auth-profile-insights"))

        self.assertEqual(response.status_code, 401)

    def test_profile_insights_aggregates_reader_activity(self):
        genre = Genre.objects.create(name="Fantasy")
        story = Story.objects.create(title="A Read Book", slug="a-read-book")
        story.genres.add(genre)
        chapter = Chapter.objects.create(
            story=story,
            title="Chapter One",
            slug="chapter-one",
            content="Once upon a time",
            order=1,
        )
        audio = Audio.objects.create(
            story=story,
            title="Audio One",
            slug="audio-one",
            audio_file="story_audios/audio-one.mp3",
            order=1,
        )
        ReadingProgress.objects.create(
            user=self.user,
            story=story,
            chapter=chapter,
            progress=1,
        )
        ChapterReadingProgress.objects.create(
            user=self.user,
            story=story,
            chapter=chapter,
            progress=1,
        )
        FileReadingProgress.objects.create(
            user=self.user,
            story=story,
            format="epub",
            progress=0.5,
        )
        AudioReadingProgress.objects.create(
            user=self.user,
            story=story,
            audio=audio,
            progress=0.25,
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-profile-insights"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["titles_started"], 1)
        self.assertEqual(response.data["summary"]["titles_completed"], 1)
        self.assertEqual(response.data["summary"]["favorite_genre"], "Fantasy")
        self.assertEqual(response.data["summary"]["active_days_30"], 1)
        formats = {item["name"]: item["value"] for item in response.data["formats"]}
        self.assertEqual(formats["Chapters"], 1)
        self.assertEqual(formats["EPUB"], 1)
        self.assertEqual(formats["PDF"], 0)
        self.assertEqual(formats["Audio"], 1)
        self.assertEqual(response.data["genres"], [{"name": "Fantasy", "value": 1}])
        self.assertEqual(response.data["activity"][-1]["reading"], 2)
        self.assertEqual(response.data["activity"][-1]["listening"], 1)

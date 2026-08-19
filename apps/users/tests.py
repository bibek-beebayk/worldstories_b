from django.contrib.auth import get_user_model
from django.core.cache import cache
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


class LoginThrottleApiTests(APITestCase):
    """Pins the fix for a previously-missing brute-force guard: login and
    admin-login had no rate limiting at all, so a scripted client could
    attempt unlimited password guesses. Both actions now share a "login"
    ScopedRateThrottle scope (5/min per IP, see DEFAULT_THROTTLE_RATES)."""

    def setUp(self):
        # LocMemCache (DRF throttling's backing store) isn't reset between
        # test methods by default — without this, an earlier test's attempts
        # would count against this one's budget depending on run order.
        cache.clear()

    def test_admin_login_is_throttled_after_five_attempts_per_ip(self):
        for _ in range(5):
            response = self.client.post(
                reverse("auth-admin-login"), {"email": "x@example.com", "password": "wrong"}
            )
            self.assertNotEqual(response.status_code, 429)

        throttled_response = self.client.post(
            reverse("auth-admin-login"), {"email": "x@example.com", "password": "wrong"}
        )
        self.assertEqual(throttled_response.status_code, 429)

    def test_login_and_admin_login_share_the_same_throttle_budget(self):
        for _ in range(5):
            self.client.post(reverse("auth-login"), {"email": "x@example.com", "password": "wrong"})

        throttled_response = self.client.post(
            reverse("auth-admin-login"), {"email": "x@example.com", "password": "wrong"}
        )
        self.assertEqual(throttled_response.status_code, 429)

    def test_unrelated_endpoints_are_not_affected_by_the_login_scope(self):
        for _ in range(5):
            self.client.post(reverse("auth-admin-login"), {"email": "x@example.com", "password": "wrong"})

        unrelated_response = self.client.get(reverse("story-list"))
        self.assertNotEqual(unrelated_response.status_code, 429)


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


class UserAdminApiTests(APITestCase):
    def setUp(self):
        self.superuser = User.objects.create_user(
            email="admin@example.com", username="admin", password="x",
            is_staff=True, is_superuser=True,
        )
        self.reader = User.objects.create_user(
            email="reader@example.com", username="reader", password="x",
        )

    def test_requires_superuser(self):
        self.client.force_authenticate(self.reader)

        response = self.client.get(reverse("admin-user-list"))

        self.assertEqual(response.status_code, 403)

    def test_lists_and_searches_users(self):
        self.client.force_authenticate(self.superuser)

        list_response = self.client.get(reverse("admin-user-list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["pagination"]["count"], 2)

        search_response = self.client.get(reverse("admin-user-list"), {"search": "reader"})
        emails = [row["email"] for row in search_response.data["results"]]
        self.assertEqual(emails, ["reader@example.com"])

    def test_promotes_another_user_to_staff(self):
        self.client.force_authenticate(self.superuser)

        response = self.client.patch(
            reverse("admin-user-detail", args=[self.reader.pk]), {"is_staff": True}
        )

        self.assertEqual(response.status_code, 200)
        self.reader.refresh_from_db()
        self.assertTrue(self.reader.is_staff)

    def test_deactivates_another_user(self):
        self.client.force_authenticate(self.superuser)

        response = self.client.patch(
            reverse("admin-user-detail", args=[self.reader.pk]), {"is_active": False}
        )

        self.assertEqual(response.status_code, 200)
        self.reader.refresh_from_db()
        self.assertFalse(self.reader.is_active)

    def test_blocks_deactivating_your_own_account(self):
        self.client.force_authenticate(self.superuser)

        response = self.client.patch(
            reverse("admin-user-detail", args=[self.superuser.pk]), {"is_active": False}
        )

        self.assertEqual(response.status_code, 400)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    def test_blocks_demoting_your_own_account(self):
        self.client.force_authenticate(self.superuser)

        response = self.client.patch(
            reverse("admin-user-detail", args=[self.superuser.pk]), {"is_superuser": False}
        )

        self.assertEqual(response.status_code, 400)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_superuser)

    def test_blocks_self_lockout_even_when_form_encoded(self):
        # Regression test: the guard used to read raw request.data, where a
        # multipart-encoded payload carries "False" as a string rather than
        # the boolean False — silently bypassing the check for any client
        # that doesn't send strict JSON.
        self.client.force_authenticate(self.superuser)

        response = self.client.patch(
            reverse("admin-user-detail", args=[self.superuser.pk]),
            {"is_active": False},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    def test_delete_is_not_allowed(self):
        self.client.force_authenticate(self.superuser)

        response = self.client.delete(reverse("admin-user-detail", args=[self.reader.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.reader.pk).exists())

    def test_deactivated_user_cannot_authenticate_with_an_existing_token(self):
        # force_authenticate() bypasses JWTAuthentication entirely, so this
        # uses a real bearer token to actually exercise simplejwt's
        # CHECK_USER_IS_ACTIVE check on every request — confirming
        # deactivation revokes an already-issued token, not just future
        # logins.
        access_token = str(RefreshToken.for_user(self.reader).access_token)
        self.reader.is_active = False
        self.reader.save(update_fields=["is_active"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get(reverse("auth-profile-insights"))

        self.assertEqual(response.status_code, 401)

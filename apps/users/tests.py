from datetime import timedelta
from math import ceil
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.stats.models import (
    AnalyticsEvent,
    AudioReadingProgress,
    ChapterReadingProgress,
    FileReadingProgress,
    ReadingProgress,
)
from apps.story import reading_time
from apps.story.models import Audio, Chapter, Favorite, Genre, Review, Story
from apps.story.signals import recompute_chapter_reading_minutes


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


class ReadingStreakApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="streaker@example.com",
            username="streaker",
            password="test-password",
        )

    def _create_event_on(self, day_offset, event_type=AnalyticsEvent.EVENT_READING_SESSION):
        event = AnalyticsEvent.objects.create(
            event_type=event_type,
            user=self.user,
            visitor_id="streaker-browser",
        )
        AnalyticsEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(days=day_offset)
        )
        return event

    def test_reading_streak_requires_authentication(self):
        response = self.client.get(reverse("auth-reading-streak"))

        self.assertEqual(response.status_code, 401)

    def test_reading_streak_computes_from_analytics_events(self):
        # A 3-day consecutive run ending yesterday (today has no activity
        # yet, so the streak should still read as alive), plus an older,
        # disconnected listening-session day that only affects longest_streak
        # if it were part of a longer run — here it's isolated, so it
        # shouldn't change either number.
        self._create_event_on(3)
        self._create_event_on(2)
        self._create_event_on(1, event_type=AnalyticsEvent.EVENT_LISTENING_SESSION)
        self._create_event_on(10)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-reading-streak"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["current_streak"], 3)
        self.assertEqual(response.data["longest_streak"], 3)

    def test_reading_streak_is_zero_with_no_activity(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-reading-streak"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["current_streak"], 0)
        self.assertEqual(response.data["longest_streak"], 0)


class LibraryContinueReadingApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="reader2@example.com", username="reader2", password="test-password"
        )
        self.story = Story.objects.create(
            title="A Read Book", slug="a-read-book-continue", is_published=True
        )
        self.chapter = Chapter.objects.create(
            story=self.story,
            title="Chapter One",
            slug="chapter-one",
            order=1,
            content=(
                "<p>Alice was beginning to get very tired of sitting by her sister "
                "on the bank, and of having nothing to do: once or twice she had "
                "peeped into the book her sister was reading, but it had no "
                "pictures or conversations in it.</p>"
            ),
        )
        ReadingProgress.objects.create(
            user=self.user, story=self.story, chapter=self.chapter, progress=0.5
        )
        ChapterReadingProgress.objects.create(
            user=self.user, story=self.story, chapter=self.chapter, progress=0.5
        )

    def test_continue_reading_includes_a_real_excerpt(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-continue-reading"))

        self.assertEqual(response.status_code, 200)
        item = response.data["results"][0]
        self.assertTrue(item["excerpt"])
        self.assertNotIn("<p>", item["excerpt"])

    def test_continue_reading_reports_remaining_minutes(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-continue-reading"))

        item = response.data["results"][0]
        total = item["story"]["reading_time_minutes"]
        self.assertIsNotNone(total)
        # Half read, so about half the estimate is left — and never rounded
        # down to zero while there is still text to read.
        self.assertEqual(item["remaining_minutes"], ceil(total * 0.5))
        self.assertGreaterEqual(item["remaining_minutes"], 1)

    def test_remaining_minutes_is_null_when_the_story_has_no_estimate(self):
        """A story of unknown length must not claim "~0 min remaining"."""
        Chapter.objects.filter(story=self.story).update(content="")
        recompute_chapter_reading_minutes(self.story.id)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-continue-reading"))

        item = response.data["results"][0]
        self.assertIsNone(item["story"]["reading_time_minutes"])
        self.assertIsNone(item["remaining_minutes"])

    def _reader_with_in_progress_stories(self, email, count):
        user = User.objects.create_user(
            email=email, username=email.split("@")[0], password="test-password"
        )
        for index in range(count):
            story = Story.objects.create(
                title=f"{email} {index}", slug=f"{email.split('@')[0]}-{index}", is_published=True
            )
            chapter = Chapter.objects.create(
                story=story,
                title="One",
                slug=f"one-{email.split('@')[0]}-{index}",
                order=1,
                content="<p>" + ("word " * 400) + "</p>",
            )
            Chapter.objects.create(
                story=story,
                title="Two",
                slug=f"two-{email.split('@')[0]}-{index}",
                order=2,
                content="<p>more</p>",
            )
            ReadingProgress.objects.create(
                user=user, story=story, chapter=chapter, progress=0.5
            )
            ChapterReadingProgress.objects.create(
                user=user, story=story, chapter=chapter, progress=0.5
            )
        return user

    def _continue_reading_query_count(self, user):
        self.client.force_authenticate(user)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("auth-library-continue-reading"))
        self.assertEqual(response.status_code, 200)
        return len(response.data["results"]), len(captured.captured_queries)

    def test_continue_reading_query_count_does_not_grow_with_the_page(self):
        """Each card used to re-query its own author, genres, categories,
        audios, videos, review count, favourite count and chapter count —
        roughly seven queries per row. Asserting the two page sizes cost the
        same is the property that matters; the absolute number is free to
        change."""
        one = self._reader_with_in_progress_stories("one-story@example.com", 1)
        many = self._reader_with_in_progress_stories("many-stories@example.com", 7)

        one_rows, one_queries = self._continue_reading_query_count(one)
        many_rows, many_queries = self._continue_reading_query_count(many)

        self.assertEqual(one_rows, 1)
        self.assertEqual(many_rows, 7)
        self.assertEqual(many_queries, one_queries)


class CachedChapterReadingTimeTests(APITestCase):
    """Story.cached_chapter_reading_minutes is what lets list responses show a
    reading time without word-counting every chapter of every story."""

    def setUp(self):
        self.story = Story.objects.create(
            title="Cached", slug="cached-reading-time", is_published=True
        )

    def test_saving_a_chapter_populates_the_cached_estimate(self):
        Chapter.objects.create(
            story=self.story,
            title="One",
            slug="one",
            order=1,
            content="<p>" + ("word " * 400) + "</p>",
        )

        self.story.refresh_from_db()
        self.assertEqual(self.story.cached_chapter_reading_minutes, 2)

    def test_the_cached_estimate_tracks_edits_and_deletions(self):
        chapter = Chapter.objects.create(
            story=self.story,
            title="One",
            slug="one",
            order=1,
            content="<p>" + ("word " * 400) + "</p>",
        )
        second = Chapter.objects.create(
            story=self.story,
            title="Two",
            slug="two",
            order=2,
            content="<p>" + ("word " * 400) + "</p>",
        )

        self.story.refresh_from_db()
        self.assertEqual(self.story.cached_chapter_reading_minutes, 4)

        second.delete()
        self.story.refresh_from_db()
        self.assertEqual(self.story.cached_chapter_reading_minutes, 2)

        chapter.content = "<p>" + ("word " * 1200) + "</p>"
        chapter.save()
        self.story.refresh_from_db()
        self.assertEqual(self.story.cached_chapter_reading_minutes, 6)

    def test_it_agrees_with_the_live_calculation(self):
        Chapter.objects.create(
            story=self.story,
            title="One",
            slug="one",
            order=1,
            content="<p>" + ("word " * 950) + "</p>",
        )
        self.story.refresh_from_db()

        self.assertEqual(
            reading_time.story_reading_minutes_cached(self.story),
            reading_time.story_reading_minutes(self.story),
        )

    def test_it_falls_back_to_the_file_estimate_for_a_chapterless_story(self):
        self.story.cached_file_reading_minutes = 42
        self.story.save(update_fields=["cached_file_reading_minutes"])
        self.story.refresh_from_db()

        self.assertEqual(reading_time.story_reading_minutes_cached(self.story), 42)

    def test_deleting_a_story_does_not_raise_through_the_chapter_cascade(self):
        Chapter.objects.create(
            story=self.story, title="One", slug="one", order=1, content="<p>hi</p>"
        )

        self.story.delete()

        self.assertFalse(Story.objects.filter(slug="cached-reading-time").exists())

    def test_the_backfill_command_fills_stories_that_predate_the_signal(self):
        Chapter.objects.create(
            story=self.story,
            title="One",
            slug="one",
            order=1,
            content="<p>" + ("word " * 400) + "</p>",
        )
        # Simulate a row written before the signal existed.
        Story.objects.filter(pk=self.story.pk).update(cached_chapter_reading_minutes=None)

        call_command("backfill_chapter_reading_times", verbosity=0)

        self.story.refresh_from_db()
        self.assertEqual(self.story.cached_chapter_reading_minutes, 2)


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


@override_settings(GOOGLE_CLIENT_ID="test-client-id")
class GoogleLoginFirstLoginApiTests(APITestCase):
    """`is_first_login` is what the frontend uses to decide whether to show
    the genre-picker onboarding step — it must be True exactly once, on the
    request that actually creates the account."""

    def _mock_google_response(self, email, name="Test User"):
        return patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value={"email": email, "name": name},
        )

    def test_is_first_login_true_for_a_brand_new_user(self):
        with self._mock_google_response("new@example.com"):
            response = self.client.post(reverse("auth-google-login"), {"token": "fake"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["user"]["is_first_login"])

    def test_is_first_login_false_for_a_returning_user(self):
        User.objects.create_user(email="existing@example.com", username="existing", password="x")

        with self._mock_google_response("existing@example.com"):
            response = self.client.post(reverse("auth-google-login"), {"token": "fake"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["user"]["is_first_login"])

    def test_rejects_a_deactivated_user(self):
        User.objects.create_user(
            email="banned@example.com", username="banned", password="x", is_active=False
        )

        with self._mock_google_response("banned@example.com"):
            response = self.client.post(reverse("auth-google-login"), {"token": "fake"})

        self.assertEqual(response.status_code, 403)


class PreferredGenresRecommendationApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="reader@example.com", username="reader", password="x"
        )
        self.fantasy = Genre.objects.create(name="Fantasy")
        self.scifi = Genre.objects.create(name="SciFi")
        self.romance = Genre.objects.create(name="Romance")

    def _story(self, title, slug, rating, genres):
        story = Story.objects.create(title=title, slug=slug, is_published=True, rating=rating)
        story.genres.set(genres)
        return story

    def test_returns_empty_list_when_no_genres_are_set(self):
        self._story("Dragon Tale", "dragon-tale", 4.5, [self.fantasy])
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_ranks_stories_matching_more_preferred_genres_first(self):
        self.user.preferred_genres.set([self.fantasy, self.scifi])
        single_match = self._story("Dragon Tale", "dragon-tale", 4.5, [self.fantasy])
        double_match = self._story("Dragon SciFi Crossover", "dragon-scifi", 3.0, [self.fantasy, self.scifi])
        self._story("Love Story", "love-story", 5.0, [self.romance])  # unrelated genre, excluded
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = [story["title"] for story in response.data]
        self.assertEqual(titles, [double_match.title, single_match.title])

    def test_excludes_stories_the_user_has_already_favorited(self):
        self.user.preferred_genres.set([self.fantasy])
        already_favorited = self._story("Old Favorite", "old-favorite", 5.0, [self.fantasy])
        Favorite.objects.create(user=self.user, story=already_favorited)
        fresh = self._story("Fresh Pick", "fresh-pick", 4.0, [self.fantasy])
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = [story["title"] for story in response.data]
        self.assertEqual(titles, [fresh.title])


class QuickReadRecommendationApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="quickreader@example.com", username="quickreader", password="x"
        )
        self.fantasy = Genre.objects.create(name="Fantasy")
        self.user.preferred_genres.set([self.fantasy])

    def _story(self, title, slug, summary=""):
        story = Story.objects.create(title=title, slug=slug, is_published=True, summary=summary)
        story.genres.set([self.fantasy])
        return story

    def test_quick_read_only_includes_stories_with_a_summary(self):
        with_summary = self._story("Has Summary", "has-summary", summary="<p>A quick summary.</p>")
        self._story("No Summary", "no-summary")
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"), {"quick_read": "true"})

        titles = [story["title"] for story in response.data]
        self.assertEqual(titles, [with_summary.title])

    def test_quick_read_excludes_the_currently_viewed_story(self):
        current = self._story("Currently Viewing", "currently-viewing", summary="<p>x</p>")
        other = self._story("Another Quick Read", "another-quick-read", summary="<p>y</p>")
        self.client.force_authenticate(self.user)

        response = self.client.get(
            reverse("auth-library-recommendations"), {"quick_read": "true", "exclude": current.slug}
        )

        titles = [story["title"] for story in response.data]
        self.assertEqual(titles, [other.title])

    def test_without_quick_read_param_includes_stories_regardless_of_summary(self):
        self._story("Has Summary", "has-summary", summary="<p>x</p>")
        self._story("No Summary", "no-summary")
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        self.assertEqual(len(response.data), 2)


class BlendedRecommendationApiTests(APITestCase):
    """Covers the warm path: once a user has crossed SUFFICIENT_DATA_THRESHOLD
    engaged stories, recommendations should also draw on implicit genre
    affinity (what they actually engaged with, not just what they picked)
    and a collaborative signal (what similar users engaged with)."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="reader@example.com", username="reader", password="x"
        )
        self.horror = Genre.objects.create(name="Horror")
        self.romance = Genre.objects.create(name="Romance")

    def _story(self, title, slug, rating, genres):
        story = Story.objects.create(title=title, slug=slug, is_published=True, rating=rating)
        story.genres.set(genres)
        return story

    def test_stays_on_the_genre_only_path_below_the_sufficient_data_threshold(self):
        self.user.preferred_genres.set([self.horror])
        engaged = [self._story(f"Engaged {i}", f"engaged-{i}", 4.0, [self.horror]) for i in range(2)]
        for story in engaged:
            Favorite.objects.create(user=self.user, story=story)

        # A neighbor who overlaps on those 2 stories and also likes something
        # outside horror — this should NOT surface yet, since 2 engaged
        # stories is below the threshold and the collaborative path isn't
        # active.
        neighbor = User.objects.create_user(email="n@example.com", username="neighbor", password="x")
        for story in engaged:
            Favorite.objects.create(user=neighbor, story=story)
        collaborative_only_pick = self._story("Collab Pick", "collab-pick", 5.0, [self.romance])
        Favorite.objects.create(user=neighbor, story=collaborative_only_pick)

        fresh_horror = self._story("Fresh Horror", "fresh-horror", 3.0, [self.horror])
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = {story["title"] for story in response.data}
        self.assertIn(fresh_horror.title, titles)
        self.assertNotIn(collaborative_only_pick.title, titles)

    def test_implicit_genre_affinity_kicks_in_without_any_explicit_preference(self):
        # No preferred_genres set at all — the signal has to come purely
        # from what the user actually engaged with.
        engaged = [self._story(f"Horror {i}", f"horror-{i}", 4.0, [self.horror]) for i in range(3)]
        for story in engaged:
            Favorite.objects.create(user=self.user, story=story)
        fresh_horror = self._story("New Horror", "new-horror", 3.5, [self.horror])
        self._story("Unrelated Romance", "unrelated-romance", 5.0, [self.romance])
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = [story["title"] for story in response.data]
        self.assertIn(fresh_horror.title, titles)
        self.assertNotIn("Unrelated Romance", titles)

    def test_collaborative_signal_surfaces_stories_outside_genre_affinity(self):
        shared = [self._story(f"Shared {i}", f"shared-{i}", 4.0, [self.horror]) for i in range(3)]
        for story in shared:
            Favorite.objects.create(user=self.user, story=story)

        neighbor = User.objects.create_user(email="n@example.com", username="neighbor", password="x")
        for story in shared:
            Favorite.objects.create(user=neighbor, story=story)
        # The neighbor also liked something in a genre this user has no
        # affinity for at all — collaborative filtering should still surface
        # it, since it isn't gated on genre match.
        cross_genre_pick = self._story("Neighbor's Pick", "neighbor-pick", 3.0, [self.romance])
        Favorite.objects.create(user=neighbor, story=cross_genre_pick)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = [story["title"] for story in response.data]
        self.assertIn(cross_genre_pick.title, titles)

    def test_a_reviewed_story_counts_as_engaged_and_is_excluded(self):
        engaged = [self._story(f"Horror {i}", f"horror-{i}", 4.0, [self.horror]) for i in range(2)]
        for story in engaged:
            Favorite.objects.create(user=self.user, story=story)
        reviewed_only = self._story("Reviewed Only", "reviewed-only", 4.5, [self.horror])
        Review.objects.create(user=self.user, story=reviewed_only, rating=5)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = [story["title"] for story in response.data]
        self.assertNotIn(reviewed_only.title, titles)

    def test_sub_threshold_progress_still_excludes_but_does_not_count_toward_sufficiency(self):
        engaged = [self._story(f"Horror {i}", f"horror-{i}", 4.0, [self.horror]) for i in range(2)]
        for story in engaged:
            Favorite.objects.create(user=self.user, story=story)
        # Barely started (10% in) — should still be excluded from being
        # re-recommended, but shouldn't count as real "engagement" toward
        # the collaborative path activating.
        barely_started = self._story("Barely Started", "barely-started", 4.0, [self.horror])
        ReadingProgress.objects.create(user=self.user, story=barely_started, progress=0.1)

        neighbor = User.objects.create_user(email="n@example.com", username="neighbor", password="x")
        for story in engaged:
            Favorite.objects.create(user=neighbor, story=story)
        Favorite.objects.create(user=neighbor, story=barely_started)
        collaborative_only_pick = self._story("Collab Pick", "collab-pick", 5.0, [self.romance])
        Favorite.objects.create(user=neighbor, story=collaborative_only_pick)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-library-recommendations"))

        titles = [story["title"] for story in response.data]
        self.assertNotIn(barely_started.title, titles)
        # Still below the 3-story threshold (the sub-threshold progress row
        # doesn't count), so the collaborative-only pick shouldn't surface.
        self.assertNotIn(collaborative_only_pick.title, titles)


class UsernameAvailabilityApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="alice@example.com", username="alice", password="x"
        )
        self.other = User.objects.create_user(
            email="bob@example.com", username="bob", password="x"
        )

    def test_requires_authentication(self):
        response = self.client.get(reverse("auth-check-username"), {"username": "alice"})

        self.assertEqual(response.status_code, 401)

    def test_reports_a_taken_username_as_unavailable(self):
        self.client.force_authenticate(self.other)

        response = self.client.get(reverse("auth-check-username"), {"username": "alice"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["available"])

    def test_reports_a_free_username_as_available(self):
        self.client.force_authenticate(self.other)

        response = self.client.get(reverse("auth-check-username"), {"username": "brand-new-name"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["available"])

    def test_does_not_flag_your_own_current_username_as_taken(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-check-username"), {"username": "alice"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["available"])

    def test_requires_a_non_empty_username(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-check-username"), {"username": "  "})

        self.assertEqual(response.status_code, 400)

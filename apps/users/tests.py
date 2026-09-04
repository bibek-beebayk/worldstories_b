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
    StoryCompletion,
    VideoWatchProgress,
)
from apps.story import reading_time
from apps.story.models import Audio, Chapter, Favorite, Genre, Review, Story
from apps.story.signals import recompute_chapter_reading_minutes
from apps.users.recommendations import (
    PRIMARY_WEIGHTS,
    reader_taste,
    select_primary_recommendation,
)


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
        # titles_completed reads the durable StoryCompletion record now rather
        # than re-deriving completion from progress counts, so the finish has
        # to be recorded the way the progress endpoints record it.
        StoryCompletion.objects.create(
            user=self.user, story=story, source=StoryCompletion.SOURCE_CHAPTERS
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("auth-profile-insights"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["titles_started"], 1)
        self.assertEqual(response.data["summary"]["titles_completed"], 1)
        self.assertEqual(response.data["summary"]["favorite_genre"], "Fantasy")
        self.assertEqual(response.data["summary"]["active_days_30"], 1)
        # Reading Journey fields (§4.3/§4.4). Reading time is measured from
        # session events, and this reader has none — zero, not an estimate
        # derived from the story's length.
        self.assertEqual(response.data["summary"]["total_reading_minutes"], 0)
        self.assertEqual(response.data["summary"]["countries_explored"], 0)
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


class PrimaryRecommendationTests(APITestCase):
    """The single "Read Next" pick, and the preference order behind it.

    Each test isolates one rung of the order from §2.2 by making the
    alternatives equal on everything else, so a failure names the rung that
    broke rather than "the ranking changed".
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="picker@example.com", username="picker", password="test-password"
        )
        self.genre = Genre.objects.create(name="Folklore", slug="folklore-primary")
        self.other_genre = Genre.objects.create(name="Myth", slug="myth-primary")
        self.finished = self._story("the-finished-one", country="JP", minutes=20)
        self.finished.genres.add(self.genre)

    def _story(self, slug, country="", minutes=None, genres=None, rating=0.0):
        story = Story.objects.create(
            title=slug.replace("-", " ").title(),
            slug=slug,
            is_published=True,
            country=country,
            rating=rating,
        )
        if genres:
            story.genres.set(genres)
        if minutes is not None:
            Story.objects.filter(pk=story.pk).update(cached_chapter_reading_minutes=minutes)
            story.refresh_from_db()
        return story

    def _pick(self):
        return select_primary_recommendation(self.user, self.finished)

    def test_unread_beats_everything_else_combined(self):
        """Unread is the top of the preference order, so it has to outrank a
        candidate that wins on every other signal at once."""
        perfect_but_read = self._story(
            "read-already", country="JP", minutes=20, genres=[self.genre], rating=5.0
        )
        StoryCompletion.objects.create(
            user=self.user, story=perfect_but_read, source=StoryCompletion.SOURCE_CHAPTERS
        )
        plain_unread = self._story("unread-plain", genres=[self.other_genre])

        self.assertEqual(self._pick(), plain_unread)

    def test_an_unstarted_story_beats_one_merely_started(self):
        started = self._story("started", country="JP", minutes=20, genres=[self.genre])
        ReadingProgress.objects.create(user=self.user, story=started, progress=0.6)
        untouched = self._story("untouched", genres=[self.genre])

        self.assertEqual(self._pick(), untouched)

    def test_a_started_story_beats_a_finished_one(self):
        finished_before = self._story("finished-before", genres=[self.genre])
        StoryCompletion.objects.create(
            user=self.user, story=finished_before, source=StoryCompletion.SOURCE_CHAPTERS
        )
        started = self._story("half-done", genres=[self.genre])
        ReadingProgress.objects.create(user=self.user, story=started, progress=0.5)

        self.assertEqual(self._pick(), started)

    def test_shared_genre_outranks_country_when_both_are_unread(self):
        same_genre = self._story("same-genre", genres=[self.genre])
        same_country_only = self._story(
            "same-country", country="JP", genres=[self.other_genre]
        )

        self.assertEqual(self._pick(), same_genre)
        self.assertNotEqual(self._pick(), same_country_only)

    def test_country_breaks_a_tie_between_equally_similar_stories(self):
        elsewhere = self._story("elsewhere", country="FR", genres=[self.genre])
        same_country = self._story("same-country", country="JP", genres=[self.genre])

        self.assertEqual(self._pick(), same_country)

    def test_a_similar_length_breaks_a_tie(self):
        wildly_longer = self._story("epic", minutes=300, genres=[self.genre])
        about_the_same = self._story("comparable", minutes=21, genres=[self.genre])

        self.assertEqual(self._pick(), about_the_same)

    def test_the_readers_own_taste_breaks_a_remaining_tie(self):
        self.user.preferred_genres.add(self.other_genre)
        plain = self._story("plain", genres=[self.genre])
        to_taste = self._story("to-taste", genres=[self.genre, self.other_genre])

        self.assertEqual(self._pick(), to_taste)

    def test_it_never_picks_the_story_just_finished(self):
        self._story("an-alternative", genres=[self.genre])

        self.assertNotEqual(self._pick(), self.finished)

    def test_it_never_picks_a_translation_of_the_story_just_finished(self):
        """Offering the same tale in another language reads as a bug."""
        translation = self._story("same-tale-in-french", genres=[self.genre])
        Story.objects.filter(pk=translation.pk).update(
            translation_group=self.finished.translation_group, language="fr"
        )

        self.assertIsNone(self._pick())

    def test_it_returns_none_only_when_there_is_genuinely_nothing(self):
        self.assertIsNone(self._pick())

    def test_it_works_for_a_signed_out_reader(self):
        candidate = self._story("for-anyone", genres=[self.genre])

        self.assertEqual(select_primary_recommendation(None, self.finished), candidate)

    def test_the_weights_stay_readable_and_ordered(self):
        """The requirements document asks for a transparent, configurable
        table — this pins the two properties the ordering depends on."""
        weights = PRIMARY_WEIGHTS

        self.assertLess(weights["already_completed"], weights["already_started"])
        self.assertLess(weights["already_started"], 0)
        # The "already met this" penalties must outweigh every positive signal
        # added together, or unread would stop being the top preference.
        positives = sum(value for value in weights.values() if value > 0)
        self.assertGreater(abs(weights["already_completed"]), positives)


class ReaderTasteSignalTests(APITestCase):
    """The §3.3 signals layered on top of the existing genre/collaborative
    ranking. Each is isolated so a failure names the signal that broke."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="taste@example.com", username="taste", password="test-password"
        )
        self.folklore = Genre.objects.create(name="Folklore", slug="folklore-taste")
        self.myth = Genre.objects.create(name="Myth", slug="myth-taste")

    def _story(self, slug, country="", minutes=None, genres=None):
        story = Story.objects.create(title=slug, slug=slug, is_published=True, country=country)
        if genres:
            story.genres.set(genres)
        if minutes is not None:
            Story.objects.filter(pk=story.pk).update(cached_chapter_reading_minutes=minutes)
            story.refresh_from_db()
        return story

    def test_a_finished_genre_counts_for_more_than_an_untouched_one(self):
        finished = self._story("finished-folklore", genres=[self.folklore])
        StoryCompletion.objects.create(
            user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
        )
        taste = reader_taste(self.user)

        matching = self._story("more-folklore", genres=[self.folklore])
        unrelated = self._story("some-myth", genres=[self.myth])

        self.assertGreater(taste.bonus_for(matching), taste.bonus_for(unrelated))

    def test_a_favourited_genre_counts(self):
        favourited = self._story("saved-myth", genres=[self.myth])
        Favorite.objects.create(user=self.user, story=favourited)
        taste = reader_taste(self.user)

        self.assertGreater(
            taste.bonus_for(self._story("more-myth", genres=[self.myth])),
            taste.bonus_for(self._story("plain", genres=[self.folklore])),
        )

    def test_a_country_they_finish_stories_from_counts(self):
        finished = self._story("from-japan", country="JP")
        StoryCompletion.objects.create(
            user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
        )
        taste = reader_taste(self.user)

        self.assertGreater(
            taste.bonus_for(self._story("another-japan", country="JP")),
            taste.bonus_for(self._story("from-france", country="FR")),
        )

    def test_a_country_they_have_never_read_from_gets_a_nudge(self):
        """Keeps a list from narrowing to one region, and feeds the Passport's
        reason for existing."""
        started = self._story("from-nepal", country="NP")
        ReadingProgress.objects.create(user=self.user, story=started, progress=0.9)
        taste = reader_taste(self.user)

        somewhere_new = self._story("from-peru", country="PE")
        already_seen = self._story("more-nepal", country="NP")

        self.assertGreater(taste.bonus_for(somewhere_new), taste.bonus_for(already_seen))

    def test_a_finished_country_still_beats_an_unexplored_one(self):
        finished = self._story("beloved-japan", country="JP")
        StoryCompletion.objects.create(
            user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
        )
        taste = reader_taste(self.user)

        self.assertGreater(
            taste.bonus_for(self._story("more-japan", country="JP")),
            taste.bonus_for(self._story("brand-new-place", country="PE")),
        )

    def test_a_familiar_length_counts(self):
        for index in range(3):
            finished = self._story(f"short-{index}", minutes=10)
            StoryCompletion.objects.create(
                user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
            )
        taste = reader_taste(self.user)

        self.assertEqual(taste.typical_minutes, 10)
        self.assertGreater(
            taste.bonus_for(self._story("also-short", minutes=12)),
            taste.bonus_for(self._story("a-novella", minutes=300)),
        )

    def test_one_long_book_does_not_move_a_readers_typical_length(self):
        """Median, not mean — a reader's history is small enough that a single
        outlier would otherwise redefine their habit."""
        for index, minutes in enumerate([8, 10, 12, 600]):
            finished = self._story(f"mixed-{index}", minutes=minutes)
            StoryCompletion.objects.create(
                user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
            )

        self.assertLessEqual(reader_taste(self.user).typical_minutes, 12)

    def test_a_story_readers_tend_to_finish_scores_higher(self):
        """§12.3's "strong completion rate" — a quality signal about the story,
        not about this reader."""
        finished_often = self._story("well-finished", genres=[self.folklore])
        rarely = self._story("often-abandoned", genres=[self.folklore])
        for _ in range(6):
            AnalyticsEvent.objects.create(
                event_type=AnalyticsEvent.EVENT_STORY_STARTED,
                user=self.user, story=finished_often, visitor_id="v",
            )
            AnalyticsEvent.objects.create(
                event_type=AnalyticsEvent.EVENT_STORY_STARTED,
                user=self.user, story=rarely, visitor_id="v",
            )
        for _ in range(5):
            AnalyticsEvent.objects.create(
                event_type=AnalyticsEvent.EVENT_STORY_COMPLETED,
                user=self.user, story=finished_often, visitor_id="v",
            )
        taste = reader_taste(self.user)

        self.assertGreater(taste.bonus_for(finished_often), taste.bonus_for(rarely))

    def test_one_reader_finishing_an_unread_story_is_not_evidence_of_quality(self):
        """Without a minimum number of starts, a story with one start and one
        completion would rank as the best on the site."""
        story = self._story("barely-seen", genres=[self.folklore])
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_STORY_STARTED,
            user=self.user, story=story, visitor_id="v",
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_STORY_COMPLETED,
            user=self.user, story=story, visitor_id="v",
        )

        self.assertEqual(reader_taste(self.user).bonus_for(story), 0)

    def test_a_reader_with_no_history_gets_no_bonuses(self):
        taste = reader_taste(self.user)

        self.assertIsNone(taste.typical_minutes)
        self.assertEqual(taste.bonus_for(self._story("anything", genres=[self.folklore])), 0)

    def test_a_story_with_no_country_is_neither_rewarded_nor_punished(self):
        finished = self._story("from-japan-2", country="JP")
        StoryCompletion.objects.create(
            user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
        )
        taste = reader_taste(self.user)

        self.assertEqual(taste.bonus_for(self._story("placeless")), 0)


class ReadingJourneySummaryTests(APITestCase):
    """The §4.3 panel's numbers, and §4.4's insistence that reading time be
    measured rather than estimated."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="journey@example.com", username="journey", password="test-password"
        )
        self.client.force_authenticate(self.user)

    def _finished(self, slug, country=""):
        story = Story.objects.create(
            title=slug, slug=slug, is_published=True, country=country
        )
        StoryCompletion.objects.create(
            user=self.user, story=story, source=StoryCompletion.SOURCE_CHAPTERS
        )
        return story

    def _summary(self):
        return self.client.get(reverse("auth-profile-insights")).data["summary"]

    def test_reading_time_comes_from_real_sessions_not_story_estimates(self):
        """A reader who abandoned a long book on page two has not read it —
        summing estimated story durations would say otherwise."""
        story = Story.objects.create(title="Long", slug="a-long-one", is_published=True)
        Chapter.objects.create(
            story=story, title="One", slug="long-one", order=1,
            content="<p>" + ("word " * 20000) + "</p>",
        )
        ReadingProgress.objects.create(user=self.user, story=story, progress=0.02)
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            user=self.user, story=story, visitor_id="v", duration_seconds=300,
        )

        self.assertEqual(self._summary()["total_reading_minutes"], 5)

    def test_it_counts_listening_and_watching_time_too(self):
        story = Story.objects.create(title="Mixed", slug="mixed", is_published=True)
        for event_type, seconds in (
            (AnalyticsEvent.EVENT_READING_SESSION, 60),
            (AnalyticsEvent.EVENT_LISTENING_SESSION, 120),
            (AnalyticsEvent.EVENT_WATCHING_SESSION, 180),
        ):
            AnalyticsEvent.objects.create(
                event_type=event_type, user=self.user, story=story,
                visitor_id="v", duration_seconds=seconds,
            )

        self.assertEqual(self._summary()["total_reading_minutes"], 6)

    def test_countries_explored_counts_distinct_countries_of_finished_stories(self):
        self._finished("jp-one", country="JP")
        self._finished("jp-two", country="JP")
        self._finished("fr-one", country="FR")

        self.assertEqual(self._summary()["countries_explored"], 2)

    def test_a_finished_story_with_no_country_explores_nowhere(self):
        self._finished("placeless")

        self.assertEqual(self._summary()["countries_explored"], 0)

    def test_an_unfinished_story_does_not_explore_its_country(self):
        """§5.2: a country is explored by *completing* a story from it."""
        story = Story.objects.create(
            title="Started", slug="started-jp", is_published=True, country="JP"
        )
        ReadingProgress.objects.create(user=self.user, story=story, progress=0.5)

        self.assertEqual(self._summary()["countries_explored"], 0)

    def test_a_new_reader_gets_zeroes_rather_than_an_error(self):
        summary = self._summary()

        self.assertEqual(summary["titles_completed"], 0)
        self.assertEqual(summary["total_reading_minutes"], 0)
        self.assertEqual(summary["countries_explored"], 0)
        self.assertIsNone(summary["favorite_genre"])


class ReadingHistoryApiTests(APITestCase):
    """History is the whole record — distinct from Continue Reading (only what
    is unfinished) and Completed (only what is finished)."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="historian@example.com", username="historian", password="test-password"
        )
        self.client.force_authenticate(self.user)

    def _story(self, slug, chapters=1, audios=0):
        story = Story.objects.create(title=slug, slug=slug, is_published=True)
        for index in range(chapters):
            Chapter.objects.create(
                story=story, title=f"C{index}", slug=f"{slug}-c{index}",
                order=index + 1, content="<p>text</p>",
            )
        for index in range(audios):
            Audio.objects.create(
                story=story, title=f"A{index}", slug=f"{slug}-a{index}",
                order=index + 1, audio_file="story_audios/fake.mp3",
            )
        return story

    def _get(self):
        return self.client.get(reverse("auth-library-reading-history"))

    def test_it_lists_everything_opened_most_recent_first(self):
        older = self._story("read-long-ago")
        newer = self._story("read-yesterday")
        for story in (older, newer):
            ReadingProgress.objects.create(
                user=self.user, story=story, chapter=story.chapters.first(), progress=0.3
            )
        ReadingProgress.objects.filter(story=older).update(
            updated_at=timezone.now() - timedelta(days=30)
        )

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["story"]["slug"] for row in response.data["results"]],
            ["read-yesterday", "read-long-ago"],
        )

    def test_it_includes_finished_stories_unlike_continue_reading(self):
        story = self._story("finished-one")
        ChapterReadingProgress.objects.create(
            user=self.user, story=story, chapter=story.chapters.first(), progress=1.0
        )
        StoryCompletion.objects.create(
            user=self.user, story=story, source=StoryCompletion.SOURCE_CHAPTERS
        )

        row = self._get().data["results"][0]

        self.assertEqual(row["story"]["slug"], "finished-one")
        self.assertTrue(row["completed"])

    def test_completion_comes_from_the_record_not_the_progress_fraction(self):
        """A story can be complete while no single surface reads 100% — an
        audiobook finished after the text was barely opened."""
        story = self._story("listened-not-read", chapters=1, audios=1)
        ReadingProgress.objects.create(
            user=self.user, story=story, chapter=story.chapters.first(), progress=0.05
        )
        StoryCompletion.objects.create(
            user=self.user, story=story, source=StoryCompletion.SOURCE_AUDIO
        )

        row = self._get().data["results"][0]

        self.assertTrue(row["completed"])

    def test_progress_is_the_furthest_any_surface_reached(self):
        story = self._story("part-read", chapters=1, audios=1)
        ReadingProgress.objects.create(
            user=self.user, story=story, chapter=story.chapters.first(), progress=0.2
        )
        AudioReadingProgress.objects.create(
            user=self.user, story=story, audio=story.audios.first(), progress=0.8
        )

        row = self._get().data["results"][0]

        self.assertAlmostEqual(row["progress"], 0.8)
        self.assertFalse(row["completed"])

    def test_a_story_appears_once_however_many_surfaces_were_used(self):
        story = self._story("read-and-heard", chapters=1, audios=1)
        ReadingProgress.objects.create(
            user=self.user, story=story, chapter=story.chapters.first(), progress=0.4
        )
        AudioReadingProgress.objects.create(
            user=self.user, story=story, audio=story.audios.first(), progress=0.6
        )
        ChapterReadingProgress.objects.create(
            user=self.user, story=story, chapter=story.chapters.first(), progress=0.4
        )

        self.assertEqual(len(self._get().data["results"]), 1)

    def test_it_is_empty_for_a_reader_who_has_opened_nothing(self):
        self.assertEqual(self._get().data["results"], [])

    def test_it_requires_authentication(self):
        self.client.force_authenticate(None)

        # format="json" so DRF answers with JSON rather than rendering its
        # browsable-API template, which Django's template-capture in the test
        # client cannot copy on this Python build.
        response = self.client.get(
            reverse("auth-library-reading-history"), format="json"
        )

        self.assertIn(response.status_code, (401, 403))

    def test_query_count_does_not_grow_with_the_page(self):
        def history_cost(user, count):
            self.client.force_authenticate(user)
            for index in range(count):
                story = self._story(f"{user.username}-{index}")
                ReadingProgress.objects.create(
                    user=user, story=story, chapter=story.chapters.first(), progress=0.5
                )
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(reverse("auth-library-reading-history"))
            self.assertEqual(response.status_code, 200)
            return len(response.data["results"]), len(captured.captured_queries)

        one = User.objects.create_user(
            email="hist1@example.com", username="histone", password="test-password"
        )
        many = User.objects.create_user(
            email="hist2@example.com", username="histmany", password="test-password"
        )

        one_rows, one_queries = history_cost(one, 1)
        many_rows, many_queries = history_cost(many, 6)

        self.assertEqual((one_rows, many_rows), (1, 6))
        self.assertEqual(many_queries, one_queries)


class WeeklyRecapApiTests(APITestCase):
    """A time-boxed view over figures that already exist, not new measurement."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="recap@example.com", username="recap", password="test-password"
        )
        self.client.force_authenticate(self.user)

    def _story(self, slug, country="", genres=()):
        story = Story.objects.create(title=slug, slug=slug, is_published=True, country=country)
        if genres:
            story.genres.set(genres)
        return story

    def _complete(self, story, days_ago=0):
        completion = StoryCompletion.objects.create(
            user=self.user, story=story, source=StoryCompletion.SOURCE_CHAPTERS
        )
        if days_ago:
            StoryCompletion.objects.filter(pk=completion.pk).update(
                completed_at=timezone.now() - timedelta(days=days_ago)
            )
        return completion

    def _session(self, story, seconds, days_ago=0):
        event = AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            user=self.user, story=story, visitor_id="v", duration_seconds=seconds,
        )
        if days_ago:
            AnalyticsEvent.objects.filter(pk=event.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )

    def _recap(self):
        return self.client.get(reverse("auth-weekly-recap")).data

    def test_it_reports_the_week(self):
        genre = Genre.objects.create(name="Folklore", slug="folklore-recap")
        story = self._story("recap-one", country="JP", genres=[genre])
        self._complete(story)
        self._session(story, 600)

        recap = self._recap()

        self.assertEqual(recap["stories_completed"], 1)
        self.assertEqual(recap["minutes_read"], 10)
        self.assertEqual(recap["countries_explored"], 1)
        self.assertEqual(recap["favourite_genre"], "Folklore")
        self.assertTrue(recap["has_activity"])

    def test_it_ignores_activity_older_than_the_window(self):
        """A recap that quietly included last month would not be a recap."""
        story = self._story("recap-old")
        self._complete(story, days_ago=30)
        self._session(story, 600, days_ago=30)

        recap = self._recap()

        self.assertEqual(recap["stories_completed"], 0)
        self.assertEqual(recap["minutes_read"], 0)
        self.assertFalse(recap["has_activity"])

    def test_a_quiet_week_reports_no_activity_rather_than_zeroes_to_display(self):
        """Five zeroes tell a reader only that they did nothing."""
        recap = self._recap()

        self.assertFalse(recap["has_activity"])
        self.assertIsNone(recap["favourite_genre"])

    def test_countries_are_counted_once_each(self):
        for index in range(2):
            self._complete(self._story(f"recap-jp-{index}", country="JP"))
        self._complete(self._story("recap-fr", country="FR"))

        self.assertEqual(self._recap()["countries_explored"], 2)

    def test_a_completed_story_with_no_country_explores_nowhere(self):
        self._complete(self._story("recap-placeless"))

        self.assertEqual(self._recap()["countries_explored"], 0)

    def test_reading_time_alone_counts_as_activity(self):
        """Someone who read all week without finishing anything still had a
        week worth showing."""
        self._session(self._story("recap-unfinished"), 1800)

        recap = self._recap()

        self.assertEqual(recap["stories_completed"], 0)
        self.assertEqual(recap["minutes_read"], 30)
        self.assertTrue(recap["has_activity"])

    def test_it_counts_journeys_finished_this_week(self):
        story = self._story("recap-journey")
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_JOURNEY_COMPLETED,
            user=self.user, story=story, visitor_id="v",
        )

        recap = self._recap()

        self.assertEqual(recap["journeys_completed"], 1)
        self.assertTrue(recap["has_activity"])

    def test_it_requires_authentication(self):
        self.client.force_authenticate(None)

        response = self.client.get(reverse("auth-weekly-recap"), format="json")

        self.assertIn(response.status_code, (401, 403))

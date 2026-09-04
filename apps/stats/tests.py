from datetime import date, timedelta
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.story.models import Audio, Chapter, Genre, Video, Blog, Story, StoryView, Submission
from apps.users.models import User

from .completion import COMPLETION_THRESHOLD
from .models import (
    Achievement,
    AnalyticsEvent,
    AudioReadingProgress,
    ChapterReadingProgress,
    FileReadingProgress,
    ReadingProgress,
    StoryCompletion,
    UserAchievement,
    VideoWatchProgress,
)
from .streaks import compute_streak

# The analytics endpoints reject requests with no User-Agent (a real browser
# always sends one), so the test client has to look like one to reach them.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class AnalyticsEventApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.story = Story.objects.create(
            title="Tracked Story",
            slug="tracked-story",
            is_published=True,
        )

    def test_anonymous_event_is_recorded_idempotently(self):
        event_id = str(uuid4())
        payload = {
            "event_id": event_id,
            "event_type": AnalyticsEvent.EVENT_AD_IMPRESSION,
            "visitor_id": "anonymous-visitor",
            "session_id": "browser-session",
            "story_slug": self.story.slug,
            "metadata": {"path": "/", "size": "leaderboard"},
        }

        first = self.client.post(reverse("analytics-events"), payload, format="json")
        second = self.client.post(reverse("analytics-events"), payload, format="json")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(AnalyticsEvent.objects.filter(event_id=event_id).count(), 1)
        event = AnalyticsEvent.objects.get(event_id=event_id)
        self.assertIsNone(event.user)
        self.assertEqual(event.story, self.story)

    def test_referral_source_metadata_round_trips_untouched(self):
        event_id = str(uuid4())

        response = self.client.post(
            reverse("analytics-events"),
            {
                "event_id": event_id,
                "event_type": AnalyticsEvent.EVENT_VISIT,
                "visitor_id": "share-click-visitor",
                "metadata": {"path": "/story/tracked-story", "referral_source": "facebook"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.get(event_id=event_id)
        self.assertEqual(event.metadata.get("referral_source"), "facebook")

    def test_read_along_interaction_events_are_accepted_with_metadata(self):
        for event_type, metadata in (
            (
                AnalyticsEvent.EVENT_READ_ALONG_CUE_SEEK,
                {"format": "read_along", "audio_slug": "track-1", "target_seconds": 12.5},
            ),
            (
                AnalyticsEvent.EVENT_READ_ALONG_FOLLOW_TOGGLE,
                {"format": "read_along", "audio_slug": "track-1", "enabled": False},
            ),
        ):
            response = self.client.post(
                reverse("analytics-events"),
                {
                    "event_id": str(uuid4()),
                    "event_type": event_type,
                    "visitor_id": "read-along-visitor",
                    "story_slug": self.story.slug,
                    "metadata": metadata,
                },
                format="json",
            )

            self.assertEqual(response.status_code, 201)
            event = AnalyticsEvent.objects.get(event_id=response.data["event_id"])
            self.assertEqual(event.metadata, metadata)

    def test_authenticated_event_uses_the_request_user(self):
        user = User.objects.create_user(
            email="reader@example.com", username="reader", password="test-password"
        )
        self.client.force_authenticate(user)

        response = self.client.post(
            reverse("analytics-events"),
            {
                "event_id": str(uuid4()),
                "event_type": AnalyticsEvent.EVENT_LISTENING_SESSION,
                "visitor_id": "reader-browser",
                "duration_seconds": 90,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(AnalyticsEvent.objects.get().user, user)

    def test_blog_slug_resolves_to_blog_fk(self):
        blog = Blog.objects.create(title="Tracked Post", slug="tracked-post", content="<p>x</p>")
        event_id = str(uuid4())

        response = self.client.post(
            reverse("analytics-events"),
            {
                "event_id": event_id,
                "event_type": AnalyticsEvent.EVENT_READING_SESSION,
                "visitor_id": "blog-visitor",
                "blog_slug": blog.slug,
                "duration_seconds": 30,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.get(event_id=event_id)
        self.assertEqual(event.blog, blog)
        self.assertIsNone(event.story)

    def test_unmatched_blog_slug_leaves_blog_null(self):
        event_id = str(uuid4())

        response = self.client.post(
            reverse("analytics-events"),
            {
                "event_id": event_id,
                "event_type": AnalyticsEvent.EVENT_READING_SESSION,
                "visitor_id": "blog-visitor",
                "blog_slug": "does-not-exist",
                "duration_seconds": 30,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(AnalyticsEvent.objects.get(event_id=event_id).blog)

    def _visit_payload(self):
        return {
            "event_id": str(uuid4()),
            "event_type": AnalyticsEvent.EVENT_VISIT,
            "visitor_id": "some-visitor",
            "metadata": {"path": "/story/tracked-story"},
        }

    def test_superuser_events_are_dropped(self):
        admin = User.objects.create_user(
            email="ops@example.com",
            username="ops",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.client.force_authenticate(admin)

        response = self.client.post(
            reverse("analytics-events"), self._visit_payload(), format="json"
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(AnalyticsEvent.objects.exists())

    def test_staff_events_are_dropped(self):
        staff = User.objects.create_user(
            email="editor@example.com",
            username="editor",
            password="test-password",
            is_staff=True,
        )
        self.client.force_authenticate(staff)

        response = self.client.post(
            reverse("analytics-events"), self._visit_payload(), format="json"
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(AnalyticsEvent.objects.exists())

    def test_crawler_events_are_dropped(self):
        crawler = APIClient(
            HTTP_USER_AGENT="Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        )

        response = crawler.post(reverse("analytics-events"), self._visit_payload(), format="json")

        self.assertEqual(response.status_code, 202)
        self.assertFalse(AnalyticsEvent.objects.exists())

    def test_reading_lifecycle_events_are_accepted(self):
        """The browser-raised half of §2.5. `story_completed` is absent by
        design — the server raises that one itself."""
        for event_type in (
            AnalyticsEvent.EVENT_STORY_STARTED,
            AnalyticsEvent.EVENT_STORY_RESUMED,
            AnalyticsEvent.EVENT_STORY_PROGRESSED,
            AnalyticsEvent.EVENT_NEXT_STORY_CLICKED,
        ):
            with self.subTest(event_type=event_type):
                event_id = str(uuid4())
                response = self.client.post(
                    reverse("analytics-events"),
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "visitor_id": "lifecycle-reader",
                        "story_slug": self.story.slug,
                        "value": 0.5,
                        "metadata": {"format": "chapter"},
                    },
                    format="json",
                )

                self.assertEqual(response.status_code, 201)
                event = AnalyticsEvent.objects.get(event_id=event_id)
                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.story, self.story)
                self.assertEqual(event.value, 0.5)

    def test_quick_read_funnel_events_are_accepted(self):
        """The three steps §12.2 reads conversion from. Their own event types
        rather than metadata on `completion`, so a summary read can never be
        counted as a story finished."""
        for event_type in (
            AnalyticsEvent.EVENT_QUICK_READ_OPENED,
            AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
            AnalyticsEvent.EVENT_QUICK_READ_FULL_STORY_CLICKED,
        ):
            with self.subTest(event_type=event_type):
                event_id = str(uuid4())
                response = self.client.post(
                    reverse("analytics-events"),
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "visitor_id": "quick-reader",
                        "story_slug": self.story.slug,
                        "metadata": {"completed_summary": True},
                    },
                    format="json",
                )

                self.assertEqual(response.status_code, 201)
                event = AnalyticsEvent.objects.get(event_id=event_id)
                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.story, self.story)

    def test_a_quick_read_completion_is_not_a_story_completion(self):
        """Guards the reason these are separate event types: a story's
        completion count must not include readers who only read the summary."""
        self.client.post(
            reverse("analytics-events"),
            {
                "event_id": str(uuid4()),
                "event_type": AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
                "visitor_id": "quick-reader",
                "story_slug": self.story.slug,
            },
            format="json",
        )

        self.assertEqual(
            AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_COMPLETION).count(), 0
        )
        self.assertFalse(StoryCompletion.objects.exists())

    def test_unknown_crawler_and_blank_user_agent_events_are_dropped(self):
        for user_agent in ("SomeUnknownBot/1.0 (+http://example.com)", "python-requests/2.31.0", ""):
            with self.subTest(user_agent=user_agent):
                client = APIClient(HTTP_USER_AGENT=user_agent)
                response = client.post(
                    reverse("analytics-events"), self._visit_payload(), format="json"
                )
                self.assertEqual(response.status_code, 202)
        self.assertFalse(AnalyticsEvent.objects.exists())


class StoryViewBeaconTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.story = Story.objects.create(
            title="Beacon Story", slug="beacon-story", is_published=True
        )

    def _beacon_url(self):
        return f"/api/stories/{self.story.slug}/view/"

    def test_detail_get_does_not_count_a_view(self):
        """Under SSR the detail GET comes from the render server, not the
        visitor — counting there recorded the wrong IP and User-Agent."""
        response = self.client.get(f"/api/stories/{self.story.slug}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(StoryView.objects.count(), 0)
        self.story.refresh_from_db()
        self.assertEqual(self.story.views, 0)

    def test_beacon_counts_one_view_and_dedupes_by_ip(self):
        first = self.client.post(self._beacon_url(), REMOTE_ADDR="203.0.113.9")
        second = self.client.post(self._beacon_url(), REMOTE_ADDR="203.0.113.9")

        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 204)
        self.assertEqual(StoryView.objects.count(), 1)
        self.story.refresh_from_db()
        self.assertEqual(self.story.views, 1)

    def test_beacon_counts_distinct_visitors_separately(self):
        self.client.post(self._beacon_url(), REMOTE_ADDR="203.0.113.9")
        self.client.post(self._beacon_url(), REMOTE_ADDR="203.0.113.10")

        self.assertEqual(StoryView.objects.count(), 2)

    def test_beacon_ignores_crawlers(self):
        crawler = APIClient(HTTP_USER_AGENT="Mozilla/5.0 (compatible; bingbot/2.0)")

        response = crawler.post(self._beacon_url(), REMOTE_ADDR="203.0.113.9")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(StoryView.objects.count(), 0)

    def test_beacon_ignores_superusers(self):
        admin = User.objects.create_user(
            email="ops2@example.com",
            username="ops2",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.client.force_authenticate(admin)

        response = self.client.post(self._beacon_url(), REMOTE_ADDR="203.0.113.9")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(StoryView.objects.count(), 0)


class AdminAudienceAnalyticsApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="admin@example.com",
            username="admin",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.reader = User.objects.create_user(
            email="reader@example.com", username="reader", password="test-password"
        )
        self.story = Story.objects.create(
            title="Analytics Story",
            slug="analytics-story",
            is_published=True,
        )
        self.quick_read_story = Story.objects.create(
            title="Quick Read Story",
            slug="quick-read-story",
            is_published=True,
            summary="<p>A summary</p>",
        )
        self.blog = Blog.objects.create(
            title="Analytics Post", slug="analytics-post", content="<p>x</p>"
        )

        first_visit = AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="returning-browser",
            session_id="session-one",
            metadata={"referral_source": "facebook"},
        )
        AnalyticsEvent.objects.filter(pk=first_visit.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="returning-browser",
            session_id="session-two",
            metadata={"referral_source": "twitter"},
        )
        # Same visitor_id/day as session-two — a repeat identity, so it's a
        # no-op for visitor/returning-visitor counts — added purely to
        # exercise the "direct" fallback for a visit with no referral_source.
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="returning-browser",
            session_id="session-two-direct",
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_AD_IMPRESSION,
            visitor_id="returning-browser",
            metadata={"path": "/story/analytics-story", "size": "rectangle"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_DOWNLOAD,
            visitor_id="returning-browser",
            story=self.story,
            value=1024,
            metadata={"content_type": "epub"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_COMPLETION,
            visitor_id="returning-browser",
            story=self.story,
            metadata={"content_type": "epub"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="returning-browser",
            story=self.story,
            duration_seconds=120,
        )
        earlier_reading = AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="returning-browser",
            story=self.story,
            duration_seconds=60,
        )
        AnalyticsEvent.objects.filter(pk=earlier_reading.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION,
            visitor_id="returning-browser",
            story=self.story,
            duration_seconds=180,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="returning-browser",
            blog=self.blog,
            duration_seconds=90,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="returning-browser",
            story=self.quick_read_story,
            duration_seconds=45,
            metadata={"format": "quick_read"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_AD_IMPRESSION,
            visitor_id="returning-browser",
            metadata={"path": "/blog/analytics-post", "size": "banner", "content_type": "blog"},
        )

    def test_superuser_receives_aggregated_audience_analytics(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-audience"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["visitors"], 1)
        self.assertEqual(response.data["summary"]["returning_visitors"], 1)
        # 2 ad_impression events now: the original story-page one, plus the
        # blog one added for this test class's blog/quick-read coverage.
        self.assertEqual(response.data["summary"]["ad_impressions"], 2)
        self.assertEqual(response.data["summary"]["downloads"], 1)
        self.assertEqual(response.data["summary"]["completions"], 1)
        self.assertEqual(response.data["summary"]["readers"], 1)
        self.assertEqual(response.data["summary"]["returning_readers"], 1)
        self.assertEqual(response.data["summary"]["reader_retention_rate"], 1)
        # completed_titles (only self.story) intersected with engaged_titles
        # (self.story + self.quick_read_story, now that a reading_session on
        # a second story exists) — 1/2, not 1/1 as before this class also
        # covered quick-read reading sessions.
        self.assertEqual(response.data["summary"]["completion_rate"], 0.5)
        # 120 + 60 (original chapter reads) + 90 (blog) + 45 (quick read) = 315s = 5.25min
        self.assertEqual(response.data["summary"]["reading_minutes"], 5.2)
        self.assertEqual(response.data["summary"]["listening_minutes"], 3)
        self.assertEqual(response.data["summary"]["blog_reading_minutes"], 1.5)
        self.assertEqual(response.data["summary"]["quick_read_reading_minutes"], 0.8)
        self.assertEqual(response.data["top_downloads"][0]["slug"], self.story.slug)
        self.assertEqual(response.data["top_blogs_read"][0]["slug"], self.blog.slug)
        self.assertEqual(response.data["top_blogs_read"][0]["minutes"], 1.5)
        content_types = {row["content_type"]: row["count"] for row in response.data["ad_impressions_by_content_type"]}
        self.assertEqual(content_types.get("blog"), 1)
        self.assertEqual(content_types.get("unknown"), 1)
        referral_sources = {row["referral_source"]: row["count"] for row in response.data["referral_sources"]}
        self.assertEqual(referral_sources.get("facebook"), 1)
        self.assertEqual(referral_sources.get("twitter"), 1)
        self.assertEqual(referral_sources.get("direct"), 1)

    def test_watching_sessions_feed_watch_metrics(self):
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_WATCHING_SESSION,
            visitor_id="returning-browser",
            story=self.story,
            duration_seconds=240,
        )
        cache.clear()
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-audience"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["watching_minutes"], 4)
        self.assertEqual(response.data["top_watched"][0]["slug"], self.story.slug)
        self.assertEqual(response.data["top_watched"][0]["minutes"], 4)
        self.assertTrue(
            any(day["watching_minutes"] for day in response.data["daily_activity"])
        )

    def test_read_along_listening_is_split_out_without_carve_out(self):
        # The setUp listening_session (180s, no format) is plain audiobook.
        # Add a read-along one on the same story.
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION,
            visitor_id="returning-browser",
            story=self.story,
            duration_seconds=120,
            metadata={"format": "read_along"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_COMPLETION,
            visitor_id="returning-browser",
            story=self.story,
            metadata={"content_type": "read_along"},
        )
        cache.clear()
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-audience"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        summary = response.data["summary"]
        # 180 + 120 = 300s = 5.0min total listening; 2.0 of it is read-along.
        self.assertEqual(summary["listening_minutes"], 5)
        self.assertEqual(summary["read_along_listening_minutes"], 2)
        self.assertEqual(summary["read_along_sessions"], 1)

        # Additive, not a carve-out: the read-along minutes are also inside the
        # same day's listening_minutes, and read_along_minutes is on every row.
        today_rows = [
            day for day in response.data["daily_activity"] if day["read_along_minutes"]
        ]
        self.assertEqual(len(today_rows), 1)
        self.assertEqual(today_rows[0]["read_along_minutes"], 2)
        self.assertGreaterEqual(today_rows[0]["listening_minutes"], 2)
        self.assertTrue(
            all("read_along_minutes" in day for day in response.data["daily_activity"])
        )

        self.assertEqual(response.data["top_read_along"][0]["slug"], self.story.slug)
        self.assertEqual(response.data["top_read_along"][0]["minutes"], 2)

        completion_types = {
            row["content_type"]: row["count"] for row in response.data["completion_types"]
        }
        self.assertEqual(completion_types.get("read_along"), 1)

    def test_top_read_along_is_empty_without_read_along_sessions(self):
        cache.clear()
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-audience"), {"days": 30})

        self.assertEqual(response.data["summary"]["read_along_listening_minutes"], 0)
        self.assertEqual(response.data["top_read_along"], [])

    def test_story_detail_audio_block_splits_read_along_listening(self):
        Audio.objects.create(
            story=self.story, title="Track", slug="track",
            audio_file="story_audios/t.mp3", order=1,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION,
            visitor_id="returning-browser", story=self.story, duration_seconds=120,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_LISTENING_SESSION,
            visitor_id="returning-browser", story=self.story, duration_seconds=60,
            metadata={"format": "read_along"},
        )
        cache.clear()
        self.client.force_authenticate(self.admin)

        response = self.client.get(
            reverse("admin-analytics-story-detail", args=[self.story.slug]), {"days": 30}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_audio"])
        self.assertEqual(response.data["audio"]["listening_minutes"], 6)  # (180 setUp + 120 + 60) / 60
        self.assertEqual(response.data["audio"]["read_along_listening_minutes"], 1)  # 60 / 60

        hourly = self.client.get(
            reverse("admin-analytics-story-detail", args=[self.story.slug]), {"days": 1}
        )
        self.assertEqual(hourly.data["time_series"]["interval"], "hour")
        self.assertEqual(len(hourly.data["time_series"]["points"]), 24)
        self.assertEqual(
            sum(point["listens"] for point in hourly.data["time_series"]["points"]), 3
        )

    def test_non_superuser_is_forbidden(self):
        self.client.force_authenticate(self.reader)

        response = self.client.get(reverse("admin-analytics-audience"))

        self.assertEqual(response.status_code, 403)


class AdminContentAnalyticsApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="admin2@example.com",
            username="admin2",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.blog = Blog.objects.create(title="Published One", slug="published-one", content="<p>x</p>")
        Blog.objects.create(title="Published Two", slug="published-two", content="<p>x</p>")
        Blog.objects.create(
            title="Draft", slug="draft-post", content="<p>x</p>", is_published=False
        )

        self.plain_story = Story.objects.create(
            title="Plain Story", slug="plain-story", is_published=True
        )
        self.audiobook_story = Story.objects.create(
            title="Audiobook Story", slug="audiobook-story", is_published=True
        )
        Audio.objects.create(
            story=self.audiobook_story,
            title="Chapter 1",
            slug="chapter-1",
            order=1,
            audio_file="story_audios/fake.mp3",
        )
        self.watchable_story = Story.objects.create(
            title="Watchable Story", slug="watchable-story", is_published=True
        )
        Video.objects.create(
            story=self.watchable_story,
            title="Narration 1",
            slug="narration-1",
            order=1,
            youtube_url="https://youtu.be/dQw4w9WgXcQ",
            youtube_id="dQw4w9WgXcQ",
        )
        Story.objects.create(
            title="Quick Read Story",
            slug="quick-read-story",
            is_published=True,
            summary="<p>A summary</p>",
        )
        Story.objects.create(title="Unpublished Story", slug="unpublished-story", is_published=False)

    def test_superuser_receives_blog_content_analytics(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-content"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["blog_posts_count"], 2)
        self.assertGreaterEqual(sum(row["count"] for row in response.data["blog_publishing_over_time"]), 2)
        self.assertEqual(response.data["stories_count"], 4)
        self.assertEqual(response.data["audiobooks_count"], 1)
        self.assertEqual(response.data["watchable_count"], 1)
        self.assertEqual(response.data["quick_read_count"], 1)
        self.assertEqual(response.data["top_audiobooks"][0]["id"], self.audiobook_story.id)

        last_day = self.client.get(reverse("admin-analytics-content"), {"days": 1})
        self.assertEqual(last_day.status_code, 200)
        self.assertEqual(last_day.data["range_days"], 1)
        self.assertEqual(last_day.data["time_interval"], "hour")
        self.assertEqual(last_day.data["publishing_interval"], "day")

        last_90_days = self.client.get(reverse("admin-analytics-content"), {"days": 90})
        self.assertEqual(last_90_days.data["time_interval"], "week")

    def test_content_analytics_includes_ranked_story_and_blog_metrics(self):
        StoryView.objects.create(story=self.plain_story, ip_address="127.0.0.1")
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="story-reader",
            story=self.plain_story,
            duration_seconds=120,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="blog-reader",
            blog=self.blog,
            metadata={"path": f"/blog/{self.blog.slug}"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="blog-reader",
            blog=self.blog,
            duration_seconds=60,
        )
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-content"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        story = next(row for row in response.data["top_stories"] if row["id"] == self.plain_story.id)
        blog = next(row for row in response.data["top_blogs"] if row["id"] == self.blog.id)
        self.assertEqual(story["views"], 1)
        self.assertEqual(story["reads"], 1)
        self.assertEqual(story["reading_minutes"], 2)
        self.assertEqual(blog["views"], 1)
        self.assertEqual(blog["reads"], 1)

        ranking = self.client.get(
            reverse("admin-analytics-content-rankings"),
            {"kind": "blog", "days": 30, "sort": "reads"},
        )
        self.assertEqual(ranking.status_code, 200)
        self.assertEqual(ranking.data["content_type"], "blog")
        self.assertEqual(ranking.data["results"][0]["id"], self.blog.id)

    def test_rankings_exclude_operator_activity_but_keep_anonymous(self):
        """Rows written before the ingest gate existed are still in the tables,
        so the aggregators have to filter operators on read too — while leaving
        logged-out visitors (user IS NULL), who are the bulk of the audience."""
        StoryView.objects.create(story=self.plain_story, ip_address="198.51.100.1")
        StoryView.objects.create(
            story=self.plain_story, user=self.admin, ip_address="198.51.100.2"
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="real-reader",
            story=self.plain_story,
            duration_seconds=60,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="admin-browser",
            user=self.admin,
            story=self.plain_story,
            duration_seconds=600,
        )
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-content"), {"days": 30})

        story = next(row for row in response.data["top_stories"] if row["id"] == self.plain_story.id)
        self.assertEqual(story["views"], 1)
        self.assertEqual(story["reads"], 1)
        self.assertEqual(story["reading_minutes"], 1)

    def test_user_metrics_exclude_operator_accounts(self):
        User.objects.create_user(
            email="reader1@example.com",
            username="reader1",
            password="test-password",
            login_count=2,
            last_login=timezone.now(),
        )
        self.admin.login_count = 40
        self.admin.last_login = timezone.now()
        self.admin.save(update_fields=["login_count", "last_login"])
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-users"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_users"], 1)
        self.assertEqual(response.data["active_users"], 1)
        buckets = {row["bucket"]: row["count"] for row in response.data["login_frequency_buckets"]}
        self.assertEqual(buckets["1-2"], 1)
        self.assertEqual(buckets["11+"], 0)


class TimeSeriesBucketFillTests(APITestCase):
    """Every chart series must carry a row for each bucket in the range, even
    an empty one — a GROUP BY only returns buckets that had data, and the chart
    then drew a straight line across the gap at even spacing, hiding quiet
    periods entirely."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="charts@example.com",
            username="charts",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.story = Story.objects.create(
            title="Charted Story", slug="charted-story", is_published=True
        )
        self.client.force_authenticate(self.admin)

    def _dated(self, obj, when, field="created_at"):
        type(obj).objects.filter(pk=obj.pk).update(**{field: when})

    def test_daily_range_emits_every_day_including_empty_ones(self):
        view = StoryView.objects.create(story=self.story, ip_address="198.51.100.5")
        self._dated(view, timezone.now() - timedelta(days=3))

        response = self.client.get(reverse("admin-analytics-content"), {"days": 7})

        series = response.data["views_over_time"]
        # Today plus each of the 7 preceding days.
        self.assertEqual(len(series), 8)
        days = [row["day"] for row in series]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(days)), len(days))
        self.assertEqual(sum(row["count"] for row in series), 1)
        self.assertEqual([row["count"] for row in series].count(0), 7)
        expected_day = (timezone.now() - timedelta(days=3)).date()
        self.assertEqual(next(row for row in series if row["count"] == 1)["day"], expected_day)

    def test_hourly_range_emits_every_hour_including_empty_ones(self):
        view = StoryView.objects.create(story=self.story, ip_address="198.51.100.6")
        self._dated(view, timezone.now() - timedelta(hours=5))

        response = self.client.get(reverse("admin-analytics-content"), {"days": 1})

        series = response.data["views_over_time"]
        self.assertEqual(response.data["time_interval"], "hour")
        # This hour plus each of the 24 preceding ones.
        self.assertEqual(len(series), 25)
        self.assertEqual(sum(row["count"] for row in series), 1)
        self.assertEqual([row["count"] for row in series].count(0), 24)

    def test_series_is_full_length_even_with_no_data_at_all(self):
        response = self.client.get(reverse("admin-analytics-content"), {"days": 30})

        series = response.data["views_over_time"]
        self.assertEqual(len(series), 31)
        self.assertTrue(all(row["count"] == 0 for row in series))

    def test_weekly_and_monthly_ranges_are_bucketed_without_gaps(self):
        for days, expected_interval, expected_length in ((90, "week", 14), (365, "month", 13)):
            with self.subTest(days=days):
                response = self.client.get(reverse("admin-analytics-content"), {"days": days})
                series = response.data["views_over_time"]
                self.assertEqual(response.data["time_interval"], expected_interval)
                self.assertEqual(len(series), expected_length)
                days_seen = [row["day"] for row in series]
                self.assertEqual(days_seen, sorted(days_seen))
                self.assertEqual(len(set(days_seen)), len(days_seen))

    def test_every_content_and_users_series_is_filled(self):
        content = self.client.get(reverse("admin-analytics-content"), {"days": 7}).data
        users = self.client.get(reverse("admin-analytics-users"), {"days": 7}).data

        for key, payload in (
            ("views_over_time", content),
            ("publishing_over_time", content),
            ("blog_publishing_over_time", content),
            ("signups_over_time", users),
        ):
            with self.subTest(series=key):
                self.assertEqual(len(payload[key]), 8)

    def test_rating_trend_fills_average_with_none_not_zero(self):
        """A day with no reviews has no average — a 0 would render as if every
        reviewer that day gave zero stars."""
        response = self.client.get(reverse("admin-analytics-engagement"), {"days": 7})

        series = response.data["rating_trend"]
        self.assertEqual(len(series), 8)
        self.assertTrue(all(row["avg_rating"] is None for row in series))
        self.assertTrue(all(row["count"] == 0 for row in series))
        self.assertEqual(len(response.data["favorites_over_time"]), 8)

    def test_audience_series_are_filled_with_zeroed_rows(self):
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="chart-visitor",
            story=self.story,
        )

        response = self.client.get(reverse("admin-analytics-audience"), {"days": 7})

        activity = response.data["daily_activity"]
        self.assertEqual(len(activity), 8)
        for row in activity:
            self.assertEqual(
                set(row) - {"day"},
                {
                    "ad_impressions",
                    "downloads",
                    "completions",
                    "reading_minutes",
                    "listening_minutes",
                    "watching_minutes",
                    "read_along_minutes",
                },
            )
        retention = response.data["visitor_retention"]
        self.assertEqual(len(retention), 8)
        self.assertEqual(sum(row["new_visitors"] for row in retention), 1)

    def test_submissions_series_emits_every_day_for_every_status_in_range(self):
        """Two-dimensional series: the dashboard pivots these rows into one line
        per status, so a status missing from a day breaks that line the same way
        a missing day breaks a single-series chart."""
        author = User.objects.create_user(
            email="author@example.com", username="author", password="test-password"
        )
        submission = Submission.objects.create(
            user=author, title="A Submission", about="about", content="<p>x</p>"
        )
        Submission.objects.filter(pk=submission.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )

        response = self.client.get(reverse("admin-analytics-submissions"), {"days": 7})

        series = response.data["submissions_over_time"]
        statuses = {row["status"] for row in series}
        self.assertEqual(len(statuses), 1)
        self.assertEqual(len(series), 8 * len(statuses))
        self.assertEqual(sum(row["count"] for row in series), 1)
        self.assertEqual([row["count"] for row in series].count(0), 7)

    def test_geography_series_is_filled(self):
        response = self.client.get(reverse("admin-analytics-geography"), {"days": 7})

        series = response.data["logins_over_time"]
        self.assertEqual(len(series), 8)
        self.assertTrue(all(row["count"] == 0 and row["users"] == 0 for row in series))


class VideoWatchProgressApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="watcher@example.com", username="watcher", password="test-password"
        )
        self.story = Story.objects.create(
            title="Watchable", slug="watchable", is_published=True
        )
        self.video = Video.objects.create(
            story=self.story,
            title="Narration 1",
            slug="narration-1",
            order=1,
            youtube_url="https://youtu.be/dQw4w9WgXcQ",
            youtube_id="dQw4w9WgXcQ",
        )
        self.url = reverse("video-watch-progress", args=[self.story.slug])

    def test_put_creates_and_get_returns_progress(self):
        self.client.force_authenticate(self.user)

        put = self.client.put(
            self.url,
            {
                "video_slug": "narration-1",
                "progress": 0.4,
                "position_seconds": 120,
                "duration_seconds": 300,
            },
            format="json",
        )
        self.assertEqual(put.status_code, 200)
        self.assertEqual(put.data["overall_progress"], 0.4)

        get = self.client.get(self.url)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.data["video_slug"], "narration-1")
        self.assertEqual(get.data["position_seconds"], 120)

    def test_progress_only_moves_forward(self):
        self.client.force_authenticate(self.user)
        VideoWatchProgress.objects.create(
            user=self.user, story=self.story, video=self.video, progress=0.8
        )

        put = self.client.put(
            self.url,
            {"video_slug": "narration-1", "progress": 0.2},
            format="json",
        )
        self.assertEqual(put.status_code, 200)
        self.video.video_watch_progress.get(user=self.user).refresh_from_db()
        self.assertEqual(
            VideoWatchProgress.objects.get(user=self.user, video=self.video).progress, 0.8
        )

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)


class ComputeStreakTests(SimpleTestCase):
    def test_empty_activity_is_zero_zero(self):
        self.assertEqual(compute_streak(set(), date(2026, 8, 22)), (0, 0))

    def test_activity_today_only(self):
        today = date(2026, 8, 22)
        self.assertEqual(compute_streak({today}, today), (1, 1))

    def test_activity_yesterday_only_is_still_alive(self):
        today = date(2026, 8, 22)
        yesterday = today - timedelta(days=1)
        self.assertEqual(compute_streak({yesterday}, today), (1, 1))

    def test_activity_two_days_ago_is_broken_but_longest_recorded(self):
        today = date(2026, 8, 22)
        two_days_ago = today - timedelta(days=2)
        self.assertEqual(compute_streak({two_days_ago}, today), (0, 1))

    def test_consecutive_run_ending_today(self):
        today = date(2026, 8, 22)
        run = {today - timedelta(days=offset) for offset in range(5)}
        self.assertEqual(compute_streak(run, today), (5, 5))

    def test_consecutive_run_ending_yesterday_is_still_alive(self):
        today = date(2026, 8, 22)
        run = {today - timedelta(days=offset) for offset in range(1, 6)}
        self.assertEqual(compute_streak(run, today), (5, 5))

    def test_longest_and_current_are_independent(self):
        today = date(2026, 8, 22)
        # A 5-day run last month, then a gap, then a 2-day run ending yesterday.
        old_run = {date(2026, 7, 1) + timedelta(days=offset) for offset in range(5)}
        recent_run = {today - timedelta(days=offset) for offset in range(1, 3)}
        activity_dates = old_run | recent_run

        current_streak, longest_streak = compute_streak(activity_dates, today)

        self.assertEqual(current_streak, 2)
        self.assertEqual(longest_streak, 5)


class StoryCompletionTests(APITestCase):
    """Completion is settled by the server on the progress write itself.

    The mechanism it replaces deduplicated on a localStorage key, so clearing
    site data or opening the story on a second device recorded the same finish
    again — and it could only ever see chapters.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="finisher@example.com", username="finisher", password="test-password"
        )
        self.client.force_authenticate(self.user)

    def _story(self, slug, chapters=0, audios=0, videos=0):
        story = Story.objects.create(title=slug, slug=slug, is_published=True)
        for index in range(chapters):
            Chapter.objects.create(
                story=story,
                title=f"Chapter {index}",
                slug=f"{slug}-chapter-{index}",
                order=index + 1,
                content="<p>text</p>",
            )
        for index in range(audios):
            Audio.objects.create(
                story=story,
                title=f"Track {index}",
                slug=f"{slug}-audio-{index}",
                order=index + 1,
                audio_file="story_audios/fake.mp3",
            )
        for index in range(videos):
            Video.objects.create(
                story=story,
                title=f"Video {index}",
                slug=f"{slug}-video-{index}",
                order=index + 1,
                youtube_url="https://youtu.be/dQw4w9WgXcQ",
                youtube_id=f"vid{index}",
            )
        return story

    def _save_chapter(self, story, chapter, progress):
        return self.client.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": chapter.slug, "progress": progress},
            format="json",
        )

    def test_finishing_every_chapter_records_one_completion(self):
        story = self._story("two-chapters", chapters=2)
        first, second = story.chapters.order_by("order")

        partway = self._save_chapter(story, first, 1.0)
        self.assertFalse(partway.data["story_completed"])
        self.assertFalse(StoryCompletion.objects.exists())

        finished = self._save_chapter(story, second, 1.0)

        self.assertTrue(finished.data["story_completed"])
        completion = StoryCompletion.objects.get()
        self.assertEqual(completion.story, story)
        self.assertEqual(completion.source, StoryCompletion.SOURCE_CHAPTERS)

    def test_completion_is_reported_exactly_once(self):
        """`story_completed` is the "finished it just now" signal the
        completion screen and first-unlock events fire on — a re-read must not
        raise it again."""
        story = self._story("single", chapters=1)
        chapter = story.chapters.get()

        first = self._save_chapter(story, chapter, 1.0)
        again = self._save_chapter(story, chapter, 1.0)

        self.assertTrue(first.data["story_completed"])
        self.assertFalse(again.data["story_completed"])
        self.assertEqual(StoryCompletion.objects.count(), 1)

    def test_a_partly_read_story_does_not_complete(self):
        story = self._story("partial", chapters=2)
        first, _ = story.chapters.order_by("order")

        self._save_chapter(story, first, 1.0)

        self.assertFalse(StoryCompletion.objects.exists())

    def test_the_final_fraction_of_a_percent_is_scrollbar_rounding(self):
        story = self._story("nearly", chapters=1)
        chapter = story.chapters.get()

        response = self._save_chapter(story, chapter, COMPLETION_THRESHOLD)

        self.assertTrue(response.data["story_completed"])

    def test_an_audiobook_completes_by_listening(self):
        """The derivation this replaces averaged chapter progress, so a story
        with no chapters could never be completed at all."""
        story = self._story("audio-only", audios=2)
        tracks = list(story.audios.order_by("order"))

        for track in tracks:
            response = self.client.put(
                reverse("audio-reading-progress", args=[story.slug]),
                {"audio_slug": track.slug, "progress": 1.0, "position_seconds": 10, "duration_seconds": 10},
                format="json",
            )

        self.assertTrue(response.data["story_completed"])
        self.assertEqual(StoryCompletion.objects.get().source, StoryCompletion.SOURCE_AUDIO)

    def test_a_video_story_completes_by_watching(self):
        story = self._story("video-only", videos=1)
        video = story.videos.get()

        response = self.client.put(
            reverse("video-watch-progress", args=[story.slug]),
            {"video_slug": video.slug, "progress": 1.0, "position_seconds": 10, "duration_seconds": 10},
            format="json",
        )

        self.assertTrue(response.data["story_completed"])
        self.assertEqual(StoryCompletion.objects.get().source, StoryCompletion.SOURCE_VIDEO)

    def test_a_file_story_completes_only_when_the_story_has_that_file(self):
        story = self._story("file-only")
        story.epub_file = "story_epubs/book.epub"
        story.save(update_fields=["epub_file"])

        response = self.client.put(
            reverse("file-reading-progress", args=[story.slug, "epub"]),
            {"progress": 1.0, "position": "epubcfi(/6/2)"},
            format="json",
        )

        self.assertTrue(response.data["story_completed"])
        self.assertEqual(StoryCompletion.objects.get().source, StoryCompletion.SOURCE_EPUB)

    def test_a_stale_file_progress_row_cannot_complete_a_story_without_that_file(self):
        story = self._story("no-file")

        response = self.client.put(
            reverse("file-reading-progress", args=[story.slug, "epub"]),
            {"progress": 1.0, "position": "epubcfi(/6/2)"},
            format="json",
        )

        self.assertFalse(response.data["story_completed"])
        self.assertFalse(StoryCompletion.objects.exists())

    def test_a_story_with_no_content_at_all_never_completes(self):
        """Guards the vacuous-truth trap: `all()` over an empty set is true,
        which would complete every story on every surface it doesn't have."""
        story = self._story("empty")

        self.assertIsNone(
            StoryCompletion.objects.filter(user=self.user, story=story).first()
        )
        self.assertFalse(StoryCompletion.objects.exists())

    def test_finishing_on_a_second_device_does_not_duplicate(self):
        """The exact failure of the localStorage-keyed mechanism: a device
        that has never seen this story re-reports the finish."""
        story = self._story("second-device", chapters=1)
        chapter = story.chapters.get()

        self._save_chapter(story, chapter, 1.0)
        other_device = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        other_device.force_authenticate(self.user)
        response = other_device.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": chapter.slug, "progress": 1.0},
            format="json",
        )

        self.assertFalse(response.data["story_completed"])
        self.assertEqual(StoryCompletion.objects.count(), 1)

    def test_completed_library_includes_stories_without_chapters(self):
        chaptered = self._story("chaptered", chapters=1)
        audio_only = self._story("listened", audios=1)
        self._save_chapter(chaptered, chaptered.chapters.get(), 1.0)
        self.client.put(
            reverse("audio-reading-progress", args=[audio_only.slug]),
            {"audio_slug": audio_only.audios.get().slug, "progress": 1.0,
             "position_seconds": 5, "duration_seconds": 5},
            format="json",
        )

        response = self.client.get(reverse("auth-library-completed-reading"))

        self.assertEqual(response.status_code, 200)
        rows = {row["story"]["slug"]: row for row in response.data["results"]}
        self.assertEqual(set(rows), {"chaptered", "listened"})
        for row in rows.values():
            self.assertEqual(row["overall_progress"], 1.0)
        # Nothing left of a story whose length is known...
        self.assertEqual(rows["chaptered"]["remaining_minutes"], 0)
        # ...and no claim at all about one whose length isn't: the audio-only
        # story has no reading estimate, so the UI omits the line rather than
        # asserting "0 min".
        self.assertIsNone(rows["listened"]["remaining_minutes"])

    def test_finishing_raises_exactly_one_story_completed_event(self):
        """The event is raised beside the StoryCompletion row, so it inherits
        that uniqueness constraint. The client-side event it replaces
        deduplicated on a localStorage key, which a second device lacks."""
        story = self._story("event-once", chapters=1)
        chapter = story.chapters.get()

        self._save_chapter(story, chapter, 1.0)
        self._save_chapter(story, chapter, 1.0)
        other_device = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        other_device.force_authenticate(self.user)
        other_device.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": chapter.slug, "progress": 1.0},
            format="json",
        )

        events = AnalyticsEvent.objects.filter(
            event_type=AnalyticsEvent.EVENT_STORY_COMPLETED
        )
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.story, story)
        self.assertEqual(event.metadata["source"], StoryCompletion.SOURCE_CHAPTERS)
        self.assertEqual(event.visitor_id, AnalyticsEvent.SERVER_VISITOR_ID)

    def test_an_unfinished_story_raises_no_completion_event(self):
        story = self._story("event-none", chapters=2)

        self._save_chapter(story, story.chapters.order_by("order").first(), 1.0)

        self.assertFalse(
            AnalyticsEvent.objects.filter(
                event_type=AnalyticsEvent.EVENT_STORY_COMPLETED
            ).exists()
        )

    def test_the_completion_event_records_the_surface_it_was_finished_on(self):
        story = self._story("event-audio", audios=1)

        self.client.put(
            reverse("audio-reading-progress", args=[story.slug]),
            {"audio_slug": story.audios.get().slug, "progress": 1.0,
             "position_seconds": 5, "duration_seconds": 5},
            format="json",
        )

        event = AnalyticsEvent.objects.get(event_type=AnalyticsEvent.EVENT_STORY_COMPLETED)
        self.assertEqual(event.metadata["source"], StoryCompletion.SOURCE_AUDIO)

    def test_completed_library_excludes_unfinished_stories(self):
        story = self._story("unfinished", chapters=2)
        self._save_chapter(story, story.chapters.order_by("order").first(), 1.0)

        response = self.client.get(reverse("auth-library-completed-reading"))

        self.assertEqual(response.data["results"], [])


class StoryPassportTests(APITestCase):
    """Derived from completions, not stored — see apps/stats/passport.py."""

    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.user = User.objects.create_user(
            email="traveller@example.com", username="traveller", password="test-password"
        )
        self.client.force_authenticate(self.user)

    def _story(self, slug, country="", chapters=1):
        story = Story.objects.create(
            title=slug, slug=slug, is_published=True, country=country
        )
        for index in range(chapters):
            Chapter.objects.create(
                story=story, title=f"C{index}", slug=f"{slug}-c{index}",
                order=index + 1, content="<p>text</p>",
            )
        return story

    def _finish(self, story):
        return self.client.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": story.chapters.first().slug, "progress": 1.0},
            format="json",
        )

    def _passport(self):
        return self.client.get(reverse("auth-story-passport")).data

    def test_completing_a_story_explores_its_country(self):
        self._finish(self._story("a-japanese-tale", country="JP"))

        passport = self._passport()

        self.assertEqual(passport["countries_explored"], 1)
        japan = next(row for row in passport["countries"] if row["code"] == "JP")
        self.assertTrue(japan["explored"])
        self.assertEqual(japan["stories_completed"], 1)
        self.assertEqual(japan["name"], "Japan")
        self.assertIsNotNone(japan["unlocked_at"])

    def test_starting_a_story_does_not_explore_its_country(self):
        story = self._story("half-read", country="JP")
        self.client.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": story.chapters.first().slug, "progress": 0.5},
            format="json",
        )

        self.assertEqual(self._passport()["countries_explored"], 0)

    def test_the_denominator_counts_countries_with_stories_not_the_iso_list(self):
        """Telling a reader they have explored 1 of 196 would measure them
        against a catalogue that does not exist."""
        self._story("jp-one", country="JP")
        self._story("fr-one", country="FR")
        self._story("placeless")

        passport = self._passport()

        self.assertEqual(passport["countries_available"], 2)

    def test_a_first_completion_reports_the_country_it_unlocked(self):
        response = self._finish(self._story("first-from-japan", country="JP"))

        self.assertTrue(response.data["story_completed"])
        self.assertEqual(response.data["unlocked_country"], "JP")

    def test_a_second_story_from_the_same_country_unlocks_nothing(self):
        self._finish(self._story("jp-first", country="JP"))

        response = self._finish(self._story("jp-second", country="JP"))

        self.assertTrue(response.data["story_completed"])
        self.assertIsNone(response.data["unlocked_country"])

    def test_the_unlock_event_is_raised_once_per_country(self):
        self._finish(self._story("jp-a", country="JP"))
        self._finish(self._story("jp-b", country="JP"))
        self._finish(self._story("fr-a", country="FR"))

        events = AnalyticsEvent.objects.filter(
            event_type=AnalyticsEvent.EVENT_COUNTRY_UNLOCKED
        )
        self.assertEqual(
            sorted(event.metadata["country"] for event in events), ["FR", "JP"]
        )

    def test_finishing_a_story_with_no_country_unlocks_nothing(self):
        response = self._finish(self._story("placeless-tale"))

        self.assertTrue(response.data["story_completed"])
        self.assertIsNone(response.data["unlocked_country"])
        self.assertEqual(self._passport()["countries_explored"], 0)

    def test_a_new_reader_sees_the_whole_world_unexplored(self):
        self._story("jp-one", country="JP")

        passport = self._passport()

        self.assertEqual(passport["countries_explored"], 0)
        self.assertEqual(passport["countries_available"], 1)
        self.assertFalse(passport["countries"][0]["explored"])
        self.assertIsNone(passport["countries"][0]["unlocked_at"])

    def test_explored_countries_are_listed_first(self):
        self._story("fr-lots-1", country="FR")
        self._story("fr-lots-2", country="FR")
        self._finish(self._story("jp-only", country="JP"))

        codes = [row["code"] for row in self._passport()["countries"]]

        # Japan has fewer stories but is the one the reader has been to.
        self.assertEqual(codes[0], "JP")

    def test_country_detail_separates_what_is_read_from_what_is_left(self):
        finished = self._story("jp-finished", country="JP")
        self._story("jp-unread", country="JP")
        self._finish(finished)

        detail = self.client.get(
            reverse("auth-story-passport-country", kwargs={"country_code": "jp"})
        ).data

        self.assertEqual(detail["name"], "Japan")
        self.assertTrue(detail["explored"])
        self.assertEqual(detail["stories_available"], 2)
        self.assertEqual([row["slug"] for row in detail["completed"]], ["jp-finished"])
        self.assertEqual([row["slug"] for row in detail["continue_exploring"]], ["jp-unread"])

    def test_country_detail_rejects_an_unknown_code(self):
        response = self.client.get(
            reverse("auth-story-passport-country", kwargs={"country_code": "ZZ"})
        )

        self.assertEqual(response.status_code, 404)

    def test_the_passport_requires_authentication(self):
        self.client.force_authenticate(None)

        response = self.client.get(reverse("auth-story-passport"), format="json")

        self.assertIn(response.status_code, (401, 403))


class AchievementTests(APITestCase):
    """Awarded incrementally, once, and never on a read path (§6.3)."""

    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.user = User.objects.create_user(
            email="achiever@example.com", username="achiever", password="test-password"
        )
        self.client.force_authenticate(self.user)

    def _story(self, slug, country="", genres=()):
        story = Story.objects.create(
            title=slug, slug=slug, is_published=True, country=country
        )
        Chapter.objects.create(
            story=story, title="One", slug=f"{slug}-c", order=1, content="<p>text</p>"
        )
        if genres:
            story.genres.set(genres)
        return story

    def _finish(self, story):
        return self.client.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": story.chapters.first().slug, "progress": 1.0},
            format="json",
        )

    def _earned_slugs(self):
        return set(
            UserAchievement.objects.filter(user=self.user, completed=True).values_list(
                "achievement__slug", flat=True
            )
        )

    def test_the_seeded_catalogue_is_present(self):
        """Seeded by a data migration, so it exists without any fixture."""
        self.assertTrue(Achievement.objects.filter(slug="first-story", active=True).exists())
        self.assertEqual(
            Achievement.objects.filter(active=True, category="countries").count(), 3
        )

    def test_finishing_a_first_story_earns_the_first_story_achievement(self):
        response = self._finish(self._story("my-first"))

        self.assertIn("first-story", self._earned_slugs())
        self.assertEqual(
            [row["slug"] for row in response.data["unlocked_achievements"]], ["first-story"]
        )

    def test_an_achievement_is_never_awarded_twice(self):
        story = self._story("re-read-me")
        self._finish(story)

        again = self._finish(story)

        self.assertEqual(again.data["unlocked_achievements"], [])
        self.assertEqual(
            UserAchievement.objects.filter(
                user=self.user, achievement__slug="first-story"
            ).count(),
            1,
        )

    def test_re_running_the_trigger_raises_no_second_event(self):
        from apps.stats.achievements import evaluate

        story = self._story("triggered")
        self._finish(story)
        evaluate(self.user, "story_completed")
        evaluate(self.user, "story_completed")

        self.assertEqual(
            AnalyticsEvent.objects.filter(
                event_type=AnalyticsEvent.EVENT_ACHIEVEMENT_UNLOCKED,
                metadata__achievement="first-story",
            ).count(),
            1,
        )

    def test_progress_is_recorded_before_the_target_is_reached(self):
        """The profile shows "3 of 10", not just earned/not-earned."""
        for index in range(3):
            self._finish(self._story(f"story-{index}"))

        row = UserAchievement.objects.get(
            user=self.user, achievement__slug="ten-stories"
        )
        self.assertEqual(row.progress, 3)
        self.assertFalse(row.completed)

    def test_countries_and_reading_advance_from_the_same_completion(self):
        for index in range(5):
            self._finish(self._story(f"country-{index}", country=f"J{index}"[:2]))

        # Five distinct countries, five stories.
        earned = self._earned_slugs()
        self.assertIn("first-story", earned)

    def test_a_genre_achievement_counts_only_its_own_genre(self):
        folklore = Genre.objects.create(name="Folklore", slug="folklore")
        Genre.objects.create(name="Myth", slug="myth-other")
        for index in range(10):
            self._finish(self._story(f"folk-{index}", genres=[folklore]))

        self.assertIn("ten-folklore", self._earned_slugs())
        self.assertNotIn("ten-classic", self._earned_slugs())

    def test_a_genre_achievement_for_a_genre_that_does_not_exist_never_progresses(self):
        """The seeded genre slugs are examples; a site with a different
        taxonomy must not break."""
        self._finish(self._story("ungenred"))

        row = UserAchievement.objects.filter(
            user=self.user, achievement__slug="ten-adventure"
        ).first()
        self.assertTrue(row is None or row.progress == 0)

    def test_quick_read_achievements_count_distinct_summaries(self):
        from apps.stats.achievements import evaluate

        story = self._story("summary-story")
        for _ in range(3):
            AnalyticsEvent.objects.create(
                event_type=AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
                user=self.user, story=story, visitor_id="v",
            )
        evaluate(self.user, "quick_read_completed")

        row = UserAchievement.objects.get(
            user=self.user, achievement__slug="ten-quick-reads"
        )
        # Re-reading one summary is not three Quick Reads.
        self.assertEqual(row.progress, 1)

    def test_a_trigger_only_measures_what_it_could_have_moved(self):
        """§6.3: no full recalculation. Finishing a story must not go and
        count Quick Reads."""
        from apps.stats.achievements import evaluate

        story = self._story("scoped")
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
            user=self.user, story=story, visitor_id="v",
        )

        evaluate(self.user, "story_completed")

        self.assertFalse(
            UserAchievement.objects.filter(
                user=self.user, achievement__target_type="quick_reads_completed"
            ).exists()
        )

    def test_an_unknown_trigger_does_nothing(self):
        from apps.stats.achievements import evaluate

        self.assertEqual(evaluate(self.user, "something_else"), [])

    def test_an_anonymous_reader_earns_nothing(self):
        from apps.stats.achievements import evaluate
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(evaluate(AnonymousUser(), "story_completed"), [])

    def test_an_inactive_achievement_is_not_awarded(self):
        Achievement.objects.filter(slug="first-story").update(active=False)

        self._finish(self._story("quietly"))

        self.assertNotIn("first-story", self._earned_slugs())

    def test_the_achievements_endpoint_lists_progress_without_recalculating(self):
        for index in range(2):
            self._finish(self._story(f"listed-{index}"))
        # Anything the endpoint reported beyond what the triggers recorded would
        # mean it is measuring, which §6.3 rules out.
        UserAchievement.objects.filter(achievement__slug="ten-stories").update(progress=0)

        response = self.client.get(reverse("auth-achievements"))

        self.assertEqual(response.status_code, 200)
        rows = {row["slug"]: row for row in response.data["results"]}
        self.assertEqual(response.data["earned"], 1)
        self.assertTrue(rows["first-story"]["completed"])
        self.assertEqual(rows["ten-stories"]["progress"], 0)
        self.assertEqual(rows["ten-stories"]["target_value"], 10)

    def test_reported_progress_never_exceeds_the_target(self):
        for index in range(3):
            self._finish(self._story(f"capped-{index}"))

        rows = {
            row["slug"]: row
            for row in self.client.get(reverse("auth-achievements")).data["results"]
        }
        self.assertEqual(rows["first-story"]["progress"], 1)

    def test_the_achievements_endpoint_requires_authentication(self):
        self.client.force_authenticate(None)

        response = self.client.get(reverse("auth-achievements"), format="json")

        self.assertIn(response.status_code, (401, 403))

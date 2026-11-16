from datetime import date, timedelta
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.story.models import (
    Audio,
    Blog,
    Chapter,
    DailyStory,
    Genre,
    Favorite,
    Review,
    Story,
    StoryReaction,
    StoryType,
    StoryJourney,
    StoryJourneyItem,
    StoryView,
    Submission,
    Video,
)
from apps.users.models import User

from .completion import COMPLETION_THRESHOLD
from .models import (
    Achievement,
    AnalyticsEvent,
    AudioReadingProgress,
    ChapterReadingProgress,
    FileReadingProgress,
    QuickReadProgress,
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


class QuickReadAnalyticsApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.admin = User.objects.create_user(
            email="quick-admin@example.com",
            username="quick-admin",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.reader = User.objects.create_user(
            email="quick-reader@example.com",
            username="quick-reader",
            password="test-password",
        )
        self.story = Story.objects.create(
            title="Tracked Quick Read",
            slug="tracked-quick-read",
            summary="<p>A useful summary.</p>",
            is_published=True,
        )

    def test_progress_is_saved_separately_and_only_moves_forward(self):
        self.client.force_authenticate(self.reader)

        first = self.client.put(
            reverse("quick-read-progress", args=[self.story.slug]),
            {"progress": 0.72},
            format="json",
        )
        second = self.client.put(
            reverse("quick-read-progress", args=[self.story.slug]),
            {"progress": 0.25},
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["progress"], 0.72)
        self.assertEqual(
            QuickReadProgress.objects.get(user=self.reader, story=self.story).progress,
            0.72,
        )
        self.assertFalse(ReadingProgress.objects.filter(user=self.reader, story=self.story).exists())

    def test_admin_receives_per_title_quick_read_funnel_and_depth(self):
        for session_id in ("visit-one", "visit-two"):
            AnalyticsEvent.objects.create(
                event_type=AnalyticsEvent.EVENT_QUICK_READ_OPENED,
                visitor_id="same-reader",
                session_id=session_id,
                story=self.story,
            )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
            visitor_id="same-reader",
            story=self.story,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_QUICK_READ_FULL_STORY_CLICKED,
            visitor_id="same-reader",
            story=self.story,
            metadata={"completed_summary": True},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="same-reader",
            story=self.story,
            duration_seconds=150,
            metadata={"format": "quick_read"},
        )
        QuickReadProgress.objects.create(user=self.reader, story=self.story, progress=0.76)
        self.client.force_authenticate(self.admin)

        response = self.client.get(
            reverse("admin-analytics-quick-read-detail", args=[self.story.slug]),
            {"days": 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["opens"], 2)
        self.assertEqual(response.data["unique_readers"], 1)
        self.assertEqual(response.data["completions"], 1)
        self.assertEqual(response.data["completion_rate"], 0.5)
        self.assertEqual(response.data["full_story_clicks"], 1)
        self.assertEqual(response.data["full_story_conversion_rate"], 1.0)
        self.assertEqual(response.data["clicks_after_completion"], 1)
        self.assertEqual(response.data["reading_minutes"], 2.5)
        self.assertEqual(response.data["avg_progress"], 0.76)
        self.assertEqual(
            sum(point["opens"] for point in response.data["time_series"]["points"]),
            2,
        )

    def test_quick_read_analytics_is_superuser_only(self):
        self.client.force_authenticate(self.reader)
        response = self.client.get(
            reverse("admin-analytics-quick-read-detail", args=[self.story.slug])
        )
        self.assertEqual(response.status_code, 403)


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


class PerTitleLifetimeAnalyticsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="lifetime-admin@example.com", username="lifetime-admin",
            password="test-password", is_superuser=True, is_staff=True,
        )
        self.reader = User.objects.create_user(
            email="lifetime-reader@example.com", username="lifetime-reader",
            password="test-password",
        )
        self.story_type = StoryType.objects.create(name="Lifetime Type")
        self.story = Story.objects.create(
            title="Lifetime Story", slug="lifetime-story", is_published=True, views=42,
            story_type=self.story_type,
        )
        self.other_story = Story.objects.create(
            title="Other Story", slug="other-story", is_published=True,
            story_type=self.story_type,
        )
        self.client.force_authenticate(self.admin)

    def _event(self, story, visitor_id, event_type, *, age_days=0, **fields):
        event = AnalyticsEvent.objects.create(
            story=story, visitor_id=visitor_id, event_type=event_type, **fields
        )
        if age_days:
            AnalyticsEvent.objects.filter(pk=event.pk).update(
                created_at=timezone.now() - timedelta(days=age_days)
            )
        return event

    def test_all_is_unbounded_and_lifetime_is_range_independent(self):
        self._event(
            self.story, "old-reader", AnalyticsEvent.EVENT_READING_SESSION,
            age_days=400, duration_seconds=120,
        )
        self._event(
            self.story, "recent-reader", AnalyticsEvent.EVENT_READING_SESSION,
            duration_seconds=60,
        )
        self._event(self.story, "human", AnalyticsEvent.EVENT_DOWNLOAD, age_days=400)
        self._event(
            self.story, "bot", AnalyticsEvent.EVENT_DOWNLOAD,
            is_suspected_bot=True,
        )
        Chapter.objects.create(
            story=self.story, title="Chapter", slug="chapter", content="x", order=1
        )
        progress = ReadingProgress.objects.create(user=self.reader, story=self.story, progress=1)
        ReadingProgress.objects.filter(pk=progress.pk).update(
            updated_at=timezone.now() - timedelta(hours=10)
        )
        completion = StoryCompletion.objects.create(
            user=self.reader, story=self.story, source=StoryCompletion.SOURCE_CHAPTERS
        )
        StoryCompletion.objects.filter(pk=completion.pk).update(
            completed_at=timezone.now() - timedelta(hours=4)
        )
        Favorite.objects.create(user=self.reader, story=self.story)
        Review.objects.create(user=self.reader, story=self.story, rating=4)

        all_data = self.client.get(
            reverse("admin-analytics-story-detail", args=[self.story.slug]), {"days": "all"}
        ).data
        day_data = self.client.get(
            reverse("admin-analytics-story-detail", args=[self.story.slug]), {"days": 1}
        ).data

        self.assertEqual(all_data["range_days"], "all")
        self.assertEqual(all_data["reading_minutes"], 3)
        self.assertEqual(day_data["reading_minutes"], 1)
        self.assertEqual(all_data["lifetime"], day_data["lifetime"])
        self.assertEqual(all_data["lifetime"]["total_views"], 42)
        self.assertEqual(all_data["lifetime"]["total_downloads"], 1)
        self.assertEqual(all_data["lifetime"]["median_time_to_completion_hours"], 6)
        self.assertEqual(all_data["time_series"]["interval"], "month")
        self.assertEqual(len(all_data["time_series"]["points"]), 24)

    def test_referrals_and_countries_are_title_scoped_and_drop_bots(self):
        self._event(
            self.story, "story-reader", AnalyticsEvent.EVENT_VISIT,
            country_code="NP", metadata={"referral_source": " Search "},
        )
        self._event(
            self.other_story, "other-reader", AnalyticsEvent.EVENT_VISIT,
            country_code="US", metadata={"referral_source": "other"},
        )
        self._event(
            self.story, "bot", AnalyticsEvent.EVENT_VISIT,
            country_code="GB", metadata={"referral_source": "bot"},
            is_suspected_bot=True,
        )

        data = self.client.get(
            reverse("admin-analytics-story-detail", args=[self.story.slug]), {"days": 30}
        ).data

        self.assertEqual(data["referral_sources"], [{"referral_source": "search", "count": 1}])
        self.assertEqual(data["countries"], [{"country_code": "NP", "visitors": 1}])


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
        self.quick_read_story = Story.objects.create(
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
        # Two published blog posts and four published stories this period.
        self.assertEqual(response.data["publishing"]["blogs"], 2)
        self.assertEqual(response.data["publishing"]["stories"], 4)
        self.assertEqual(response.data["kpis"]["titles_published"]["value"], 6)
        self.assertEqual(response.data["top_content"]["audiobook"]["count"], 1)
        self.assertEqual(response.data["top_content"]["audiobook"]["rows"][0]["id"], self.audiobook_story.id)
        self.assertEqual(response.data["top_content"]["quick_read"]["count"], 1)
        self.assertEqual(response.data["top_content"]["blog"]["count"], 2)

        last_day = self.client.get(reverse("admin-analytics-content"), {"days": 1})
        self.assertEqual(last_day.status_code, 200)
        self.assertEqual(last_day.data["range_days"], 1)
        self.assertEqual(last_day.data["time_interval"], "hour")

        last_90_days = self.client.get(reverse("admin-analytics-content"), {"days": 90})
        self.assertEqual(last_90_days.data["time_interval"], "week")

        all_time = self.client.get(reverse("admin-analytics-content"), {"days": "all"})
        self.assertEqual(all_time.status_code, 200)
        self.assertEqual(all_time.data["range_days"], "all")
        self.assertEqual(all_time.data["time_interval"], "month")

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
        story = next(row for row in response.data["top_content"]["story"]["rows"] if row["id"] == self.plain_story.id)
        blog = next(row for row in response.data["top_content"]["blog"]["rows"] if row["id"] == self.blog.id)
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

    def test_quick_reads_have_separate_rankings_from_full_story_reads(self):
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_QUICK_READ_OPENED,
            visitor_id="quick-reader",
            story=self.quick_read_story,
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="quick-reader",
            story=self.quick_read_story,
            duration_seconds=90,
            metadata={"format": "quick_read"},
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
            visitor_id="quick-reader",
            story=self.quick_read_story,
        )
        self.client.force_authenticate(self.admin)

        quick_reads = self.client.get(
            reverse("admin-analytics-content-rankings"),
            {"kind": "quick_read", "days": 30},
        )
        stories = self.client.get(
            reverse("admin-analytics-content-rankings"),
            {"kind": "story", "days": 30},
        )

        self.assertEqual(quick_reads.status_code, 200)
        quick_row = next(
            row for row in quick_reads.data["results"]
            if row["id"] == self.quick_read_story.id
        )
        story_row = next(
            row for row in stories.data["results"]
            if row["id"] == self.quick_read_story.id
        )
        self.assertEqual(quick_row["views"], 1)
        self.assertEqual(quick_row["reads"], 1)
        self.assertEqual(quick_row["reading_minutes"], 1.5)
        self.assertEqual(quick_row["completions"], 1)
        self.assertEqual(story_row["reads"], 0)
        self.assertEqual(story_row["reading_minutes"], 0)

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

        story = next(row for row in response.data["top_content"]["story"]["rows"] if row["id"] == self.plain_story.id)
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

        series = response.data["views_over_time"]["series"]
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

        series = response.data["views_over_time"]["series"]
        self.assertEqual(response.data["time_interval"], "hour")
        # This hour plus each of the 24 preceding ones.
        self.assertEqual(len(series), 25)
        self.assertEqual(sum(row["count"] for row in series), 1)
        self.assertEqual([row["count"] for row in series].count(0), 24)

    def test_series_is_full_length_even_with_no_data_at_all(self):
        response = self.client.get(reverse("admin-analytics-content"), {"days": 30})

        series = response.data["views_over_time"]["series"]
        self.assertEqual(len(series), 31)
        self.assertTrue(all(row["count"] == 0 for row in series))

    def test_weekly_and_monthly_ranges_are_bucketed_without_gaps(self):
        for days, expected_interval, expected_length in ((90, "week", 14), (365, "month", 13)):
            with self.subTest(days=days):
                response = self.client.get(reverse("admin-analytics-content"), {"days": days})
                series = response.data["views_over_time"]["series"]
                self.assertEqual(response.data["time_interval"], expected_interval)
                self.assertEqual(len(series), expected_length)
                days_seen = [row["day"] for row in series]
                self.assertEqual(days_seen, sorted(days_seen))
                self.assertEqual(len(set(days_seen)), len(days_seen))

    def test_every_content_and_users_series_is_filled(self):
        content = self.client.get(reverse("admin-analytics-content"), {"days": 7}).data
        users = self.client.get(reverse("admin-analytics-users"), {"days": 7}).data

        for key, series in (
            ("views_over_time", content["views_over_time"]["series"]),
            ("previous views_over_time", content["views_over_time"]["previous_series"]),
            ("publishing", content["publishing"]["series"]),
            ("signups_over_time", users["signups_over_time"]),
        ):
            with self.subTest(series=key):
                self.assertEqual(len(series), 8)

    def test_feedback_sparklines_fill_every_bucket_and_an_empty_average_is_none(self):
        """A range with no reviews has no average rating: a 0 would read as every
        reviewer giving zero stars."""
        response = self.client.get(reverse("admin-analytics-engagement"), {"days": 7})

        feedback = response.data["feedback"]
        for key in ("favorites", "reviews", "reactions"):
            self.assertEqual(len(feedback[key]["sparkline"]), 8, key)
            self.assertEqual(feedback[key]["total"], 0)
        self.assertIsNone(feedback["reviews"]["avg_rating"])

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

    def test_watching_a_video_never_completes_a_story(self):
        story = self._story("video-only", videos=1)
        video = story.videos.get()

        response = self.client.put(
            reverse("video-watch-progress", args=[story.slug]),
            {"video_slug": video.slug, "progress": 1.0, "position_seconds": 10, "duration_seconds": 10},
            format="json",
        )

        self.assertNotIn("story_completed", response.data)
        self.assertFalse(StoryCompletion.objects.exists())

        response = self.client.put(
            reverse("video-progress-by-id", args=[video.id]),
            {"progress": 1.0, "position_seconds": 10, "duration_seconds": 10},
            format="json",
        )

        self.assertEqual(response.data["progress"], 1.0)
        self.assertFalse(StoryCompletion.objects.exists())

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

    def test_finishing_todays_configured_story_raises_daily_completion_once(self):
        story = self._story("daily-completion", chapters=1)
        DailyStory.objects.create(date=timezone.localdate(), story=story)
        chapter = story.chapters.get()

        self._save_chapter(story, chapter, 1.0)
        self._save_chapter(story, chapter, 1.0)

        events = AnalyticsEvent.objects.filter(
            event_type=AnalyticsEvent.EVENT_DAILY_STORY_COMPLETED, story=story
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().metadata["date"], timezone.localdate().isoformat())

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


class StoryJourneyTests(APITestCase):
    """Progress derived from completions, never stored — the same decision as
    the Story Passport, for the same reason."""

    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.user = User.objects.create_user(
            email="wanderer@example.com", username="wanderer", password="test-password"
        )
        self.client.force_authenticate(self.user)
        self.journey = StoryJourney.objects.create(
            title="Japanese Folklore", slug="japanese-folklore-test", active=True
        )

    def _story(self, slug):
        story = Story.objects.create(title=slug, slug=slug, is_published=True)
        Chapter.objects.create(
            story=story, title="One", slug=f"{slug}-c", order=1, content="<p>text</p>"
        )
        return story

    def _add(self, story, position, required=True):
        return StoryJourneyItem.objects.create(
            journey=self.journey, story=story, position=position, required=required
        )

    def _finish(self, story):
        return self.client.put(
            reverse("reading-progress", args=[story.slug]),
            {"chapter_slug": story.chapters.first().slug, "progress": 1.0},
            format="json",
        )

    def _list(self):
        return self.client.get(reverse("journey-list")).data["journeys"]

    def test_progress_counts_completed_stories(self):
        first, second = self._story("jf-one"), self._story("jf-two")
        self._add(first, 1)
        self._add(second, 2)

        self._finish(first)

        row = self._list()[0]
        self.assertEqual((row["completed"], row["total"]), (1, 2))
        self.assertFalse(row["is_complete"])

    def test_a_journey_completes_when_every_required_story_is_done(self):
        first, second = self._story("jf-a"), self._story("jf-b")
        self._add(first, 1)
        self._add(second, 2)
        self._finish(first)

        self._finish(second)

        self.assertTrue(self._list()[0]["is_complete"])

    def test_optional_items_do_not_hold_a_journey_open(self):
        """An editor adding a bonus story must not move the finish line for
        readers already part-way through."""
        required, bonus = self._story("jf-req"), self._story("jf-bonus")
        self._add(required, 1)
        self._add(bonus, 2, required=False)

        self._finish(required)

        self.assertTrue(self._list()[0]["is_complete"])

    def test_a_journey_with_no_required_items_is_never_complete(self):
        """The vacuous-truth trap: an empty journey is unfinished, not
        finished for everyone."""
        bonus = self._story("jf-only-bonus")
        self._add(bonus, 1, required=False)

        self._finish(bonus)

        self.assertEqual(self._list(), [])

    def test_an_empty_journey_is_not_shown_to_readers(self):
        self.assertEqual(self._list(), [])

    def test_an_inactive_journey_is_not_shown(self):
        story = self._story("jf-hidden")
        self._add(story, 1)
        StoryJourney.objects.filter(pk=self.journey.pk).update(active=False)

        self.assertEqual(self._list(), [])

    def test_the_first_completion_raises_journey_started(self):
        first, second = self._story("jf-s1"), self._story("jf-s2")
        self._add(first, 1)
        self._add(second, 2)

        self._finish(first)

        events = AnalyticsEvent.objects.filter(
            event_type=AnalyticsEvent.EVENT_JOURNEY_STARTED
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().metadata["journey"], self.journey.slug)

    def test_a_later_completion_does_not_re_raise_journey_started(self):
        first, second = self._story("jf-r1"), self._story("jf-r2")
        self._add(first, 1)
        self._add(second, 2)
        self._finish(first)

        self._finish(second)

        self.assertEqual(
            AnalyticsEvent.objects.filter(
                event_type=AnalyticsEvent.EVENT_JOURNEY_STARTED
            ).count(),
            1,
        )

    def test_finishing_raises_journey_completed_exactly_once(self):
        story = self._story("jf-single")
        self._add(story, 1)

        self._finish(story)
        self._finish(story)

        self.assertEqual(
            AnalyticsEvent.objects.filter(
                event_type=AnalyticsEvent.EVENT_JOURNEY_COMPLETED
            ).count(),
            1,
        )

    def test_completing_a_journey_earns_the_journey_achievement(self):
        story = self._story("jf-achieve")
        self._add(story, 1)

        response = self._finish(story)

        slugs = [row["slug"] for row in response.data["unlocked_achievements"]]
        self.assertIn("first-journey", slugs)

    def test_detail_marks_which_stories_are_done_and_keeps_order(self):
        first, second = self._story("jf-d1"), self._story("jf-d2")
        self._add(second, 2)
        self._add(first, 1)
        self._finish(first)

        detail = self.client.get(
            reverse("journey-detail", kwargs={"slug": self.journey.slug})
        ).data

        self.assertEqual([item["story"]["slug"] for item in detail["items"]], ["jf-d1", "jf-d2"])
        self.assertTrue(detail["items"][0]["completed"])
        self.assertFalse(detail["items"][1]["completed"])

    def test_detail_404s_for_a_journey_with_no_stories(self):
        response = self.client.get(
            reverse("journey-detail", kwargs={"slug": self.journey.slug})
        )

        self.assertEqual(response.status_code, 404)

    def test_a_signed_out_reader_sees_the_journey_with_no_progress(self):
        story = self._story("jf-public")
        self._add(story, 1)
        self.client.force_authenticate(None)

        rows = self.client.get(reverse("journey-list"), format="json").data["journeys"]

        self.assertEqual(rows[0]["completed"], 0)
        self.assertFalse(rows[0]["is_complete"])

    def test_the_cover_falls_back_to_the_first_story(self):
        story = self._story("jf-cover")
        story.cover_image = "https://example.test/cover.jpg"
        story.save(update_fields=["cover_image"])
        self._add(story, 1)

        self.assertTrue(self._list()[0]["cover_image"])


class EngagementMetricsApiTests(APITestCase):
    """§12.2's funnel. Ratios over events the milestones already emit."""

    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)
        self.admin = User.objects.create_user(
            email="metrics@example.com", username="metrics", password="test-password",
            is_superuser=True, is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="metricsreader@example.com", username="metricsreader", password="test-password"
        )
        self.story = Story.objects.create(title="M", slug="metrics-story", is_published=True)
        self.client.force_authenticate(self.admin)

    def _event(self, event_type, **kwargs):
        return AnalyticsEvent.objects.create(
            event_type=event_type,
            user=kwargs.pop("user", self.reader),
            story=kwargs.pop("story", self.story),
            visitor_id=kwargs.pop("visitor_id", "v"),
            **kwargs,
        )

    def _metrics(self):
        return self.client.get(reverse("admin-analytics-engagement-metrics"), {"days": 30}).data

    def _rate(self, key):
        return next(row for row in self._metrics()["funnel"] if row["key"] == key)["rate"]

    def test_completion_rate_is_completions_over_starts(self):
        for _ in range(4):
            self._event(AnalyticsEvent.EVENT_STORY_STARTED)
        self._event(AnalyticsEvent.EVENT_STORY_COMPLETED)

        self.assertEqual(self._rate("completion_rate"), 0.25)

    def test_quick_read_conversion_is_clicks_over_completions(self):
        for _ in range(2):
            self._event(AnalyticsEvent.EVENT_QUICK_READ_COMPLETED)
        self._event(AnalyticsEvent.EVENT_QUICK_READ_FULL_STORY_CLICKED)

        self.assertEqual(self._rate("quick_read_conversion"), 0.5)

    def test_a_metric_with_no_denominator_reports_none_not_zero(self):
        """"No data yet" and "0%" look identical in a dashboard and mean
        opposite things — a completion rate reading 0% because nobody started
        anything would send someone hunting a bug."""
        self.assertIsNone(self._rate("completion_rate"))
        self.assertIsNone(self._rate("journey_completion_rate"))

    def test_resume_rate_is_measured_against_all_openings(self):
        self._event(AnalyticsEvent.EVENT_STORY_STARTED)
        self._event(AnalyticsEvent.EVENT_STORY_RESUMED)
        self._event(AnalyticsEvent.EVENT_STORY_RESUMED)

        self.assertAlmostEqual(self._rate("resume_rate"), 0.6667, places=3)

    def test_countries_per_reader_divides_by_readers_who_finished_something(self):
        """Dividing by every active reader would measure how many people have
        not finished a story, not how far the ones who did have travelled."""
        for index, country in enumerate(["JP", "FR"]):
            story = Story.objects.create(
                title=f"c{index}", slug=f"metrics-c{index}", is_published=True, country=country
            )
            StoryCompletion.objects.create(
                user=self.reader, story=story, source=StoryCompletion.SOURCE_CHAPTERS
            )

        self.assertEqual(self._metrics()["averages"]["countries_per_reader"], 2.0)

    def test_stories_per_session_counts_distinct_stories(self):
        other = Story.objects.create(title="O", slug="metrics-other", is_published=True)
        for story in (self.story, self.story, other):
            self._event(
                AnalyticsEvent.EVENT_READING_SESSION,
                story=story, session_id="s1", duration_seconds=60,
            )

        self.assertEqual(self._metrics()["averages"]["stories_per_session"], 2.0)

    def test_retention_only_counts_readers_old_enough_to_have_returned(self):
        """Counting readers who have not had the chance would drag every
        retention figure toward zero as traffic grows."""
        fresh = self._event(AnalyticsEvent.EVENT_READING_SESSION)
        AnalyticsEvent.objects.filter(pk=fresh.pk).update(created_at=timezone.now())

        d7 = self._metrics()["retention"]["d7"]

        self.assertEqual(d7["eligible"], 0)
        self.assertIsNone(d7["rate"])

    def test_a_returning_reader_counts_toward_retention(self):
        first = self._event(AnalyticsEvent.EVENT_READING_SESSION)
        AnalyticsEvent.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        later = self._event(AnalyticsEvent.EVENT_READING_SESSION)
        AnalyticsEvent.objects.filter(pk=later.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        d7 = self._metrics()["retention"]["d7"]

        self.assertEqual((d7["eligible"], d7["returned"]), (1, 1))
        self.assertEqual(d7["rate"], 1.0)

    def test_discovery_counters_are_reported(self):
        self._event(AnalyticsEvent.EVENT_SURPRISE_ME_CLICKED, story=None)
        self._event(AnalyticsEvent.EVENT_MOOD_SELECTED, story=None)

        discovery = self._metrics()["discovery"]

        self.assertEqual(discovery["surprise_me_clicked"], 1)
        self.assertEqual(discovery["mood_selected"], 1)

    def test_operator_activity_is_excluded_like_every_other_dashboard_query(self):
        self._event(AnalyticsEvent.EVENT_STORY_STARTED, user=self.admin)

        self.assertIsNone(self._rate("completion_rate"))

    def test_the_endpoint_is_superuser_only(self):
        self.client.force_authenticate(self.reader)

        response = self.client.get(
            reverse("admin-analytics-engagement-metrics"), format="json"
        )

        self.assertEqual(response.status_code, 403)


# --- Request provenance (country / ip_hash / ua_family) and bot-share tools ---

import hashlib
import hmac
import io
import zipfile
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import RequestFactory

from .request_meta import country_code_from_request, hash_ip, ua_family


class RequestMetaHelperTests(SimpleTestCase):
    def _request(self, **meta):
        return RequestFactory().post("/", **meta)

    def test_country_header_parsing(self):
        cases = [
            ({"HTTP_CF_IPCOUNTRY": "NP"}, "NP"),
            ({"HTTP_CF_IPCOUNTRY": " np "}, "NP"),
            ({"HTTP_CF_IPCOUNTRY": "XX"}, ""),
            ({"HTTP_CF_IPCOUNTRY": "T1"}, ""),
            ({"HTTP_CF_IPCOUNTRY": "USA"}, ""),
            ({"HTTP_CF_IPCOUNTRY": "1A"}, ""),
            ({"HTTP_CF_IPCOUNTRY": ""}, ""),
            ({}, ""),
        ]
        for meta, expected in cases:
            with self.subTest(meta=meta):
                self.assertEqual(country_code_from_request(self._request(**meta)), expected)

    def test_ip_hash_is_stable_keyed_and_not_the_ip(self):
        first = hash_ip("203.0.113.7")
        self.assertEqual(first, hash_ip("203.0.113.7"))
        self.assertNotEqual(first, hash_ip("203.0.113.8"))
        self.assertEqual(len(first), 64)
        self.assertNotIn("203.0.113.7", first)
        expected = hmac.new(
            (getattr(settings, "ANALYTICS_IP_HASH_KEY", "") or settings.SECRET_KEY).encode(),
            b"203.0.113.7",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(first, expected)
        self.assertEqual(hash_ip(""), "")
        self.assertEqual(hash_ip(None), "")

    def test_ua_family_buckets(self):
        cases = {
            BROWSER_USER_AGENT: "chrome_desktop",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1": "safari_mobile",
            "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0": "firefox_desktop",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0": "edge_desktop",
            "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36": "samsung_mobile",
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/450.0.0.0;]": "facebook_app_mobile",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "Mobile/15E148 Instagram 320.0.0.0": "instagram_app_mobile",
            "curl/8.0": "other_desktop",
            "": "",
        }
        for user_agent, expected in cases.items():
            with self.subTest(user_agent=user_agent[:40]):
                self.assertEqual(ua_family(user_agent), expected)


class AnalyticsEventProvenanceTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER_USER_AGENT)

    def _payload(self, **overrides):
        return {
            "event_id": str(uuid4()),
            "event_type": AnalyticsEvent.EVENT_VISIT,
            "visitor_id": "provenance-visitor",
            "metadata": {"path": "/"},
            **overrides,
        }

    def test_server_derives_country_ip_hash_and_ua_family(self):
        response = self.client.post(
            reverse("analytics-events"),
            self._payload(),
            format="json",
            HTTP_CF_IPCOUNTRY="np",
            HTTP_X_FORWARDED_FOR="198.51.100.9, 10.0.0.1",
        )

        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.country_code, "NP")
        self.assertEqual(event.ip_hash, hash_ip("198.51.100.9"))
        self.assertNotEqual(event.ip_hash, "198.51.100.9")
        self.assertNotIn("198.51.100.9", event.ip_hash)
        self.assertEqual(event.ua_family, "chrome_desktop")
        self.assertFalse(event.is_suspected_bot)

    def test_ip_hash_is_stable_across_events_from_one_ip(self):
        for _ in range(2):
            self.client.post(
                reverse("analytics-events"), self._payload(event_id=str(uuid4())),
                format="json", REMOTE_ADDR="192.0.2.44",
            )
        hashes = set(AnalyticsEvent.objects.values_list("ip_hash", flat=True))
        self.assertEqual(len(hashes), 1)
        self.assertNotIn("", hashes)

    def test_unknown_or_missing_country_is_blank(self):
        for header in ({"HTTP_CF_IPCOUNTRY": "XX"}, {"HTTP_CF_IPCOUNTRY": "T1"}, {}):
            self.client.post(
                reverse("analytics-events"), self._payload(event_id=str(uuid4())),
                format="json", **header,
            )
        self.assertEqual(set(AnalyticsEvent.objects.values_list("country_code", flat=True)), {""})

    def test_client_payload_cannot_set_provenance_fields(self):
        self.client.post(
            reverse("analytics-events"),
            self._payload(
                country_code="ZZ",
                ip_hash="f" * 64,
                ua_family="forged_desktop",
                is_suspected_bot=True,
            ),
            format="json",
            HTTP_CF_IPCOUNTRY="IN",
            REMOTE_ADDR="192.0.2.50",
        )
        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.country_code, "IN")
        self.assertEqual(event.ip_hash, hash_ip("192.0.2.50"))
        self.assertEqual(event.ua_family, "chrome_desktop")
        self.assertFalse(event.is_suspected_bot)

    def test_bot_user_agent_is_still_dropped(self):
        crawler = APIClient(HTTP_USER_AGENT="Mozilla/5.0 (compatible; Googlebot/2.1)")
        response = crawler.post(
            reverse("analytics-events"), self._payload(), format="json", HTTP_CF_IPCOUNTRY="US"
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(AnalyticsEvent.objects.exists())

    def test_server_raised_events_leave_provenance_blank(self):
        event = AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_STORY_COMPLETED,
            visitor_id=AnalyticsEvent.SERVER_VISITOR_ID,
        )
        self.assertEqual((event.country_code, event.ip_hash, event.ua_family), ("", "", ""))


class FlagSuspectedBotsCommandTests(APITestCase):
    BOT_IP = "a" * 64
    BUSY_IP = "b" * 64
    SMALL_IP = "c" * 64

    def _visit(self, visitor_id, ip_hash, user=None):
        return AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id=visitor_id,
            ip_hash=ip_hash,
            user=user,
        )

    def setUp(self):
        self.user = User.objects.create_user(
            email="member@example.com", username="member", password="test-password"
        )
        # 16 visitors, visits only: the shape that should be flagged.
        for i in range(16):
            self._visit(f"bot-{i}", self.BOT_IP)
        # A logged-in row on the same IP must never be flagged.
        self.user_event = self._visit("member-browser", self.BOT_IP, user=self.user)
        # 16 visitors behind one IP (e.g. carrier NAT) but one actually read.
        for i in range(16):
            self._visit(f"nat-{i}", self.BUSY_IP)
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id="nat-3",
            ip_hash=self.BUSY_IP,
            duration_seconds=30,
        )
        # Under the threshold.
        for i in range(5):
            self._visit(f"small-{i}", self.SMALL_IP)
        # Old rows without a hash are ignored entirely.
        AnalyticsEvent.objects.create(event_type=AnalyticsEvent.EVENT_VISIT, visitor_id="legacy")

    def _run(self, *args):
        out = StringIO()
        call_command("flag_suspected_bots", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        output = self._run()
        self.assertIn("DRY RUN", output)
        self.assertIn("Would flag 16 events from 16 visitors across 1 ip_hashes", output)
        self.assertIn(self.BOT_IP[:8], output)
        self.assertFalse(AnalyticsEvent.objects.filter(is_suspected_bot=True).exists())

    def test_apply_flags_only_zero_engagement_anonymous_visitors(self):
        output = self._run("--apply")
        self.assertIn("Flagged 16 events", output)
        flagged = AnalyticsEvent.objects.filter(is_suspected_bot=True)
        self.assertEqual(flagged.count(), 16)
        self.assertTrue(all(v.startswith("bot-") for v in flagged.values_list("visitor_id", flat=True)))
        self.user_event.refresh_from_db()
        self.assertFalse(self.user_event.is_suspected_bot)
        self.assertFalse(AnalyticsEvent.objects.filter(ip_hash=self.BUSY_IP, is_suspected_bot=True).exists())
        self.assertFalse(AnalyticsEvent.objects.filter(ip_hash=self.SMALL_IP, is_suspected_bot=True).exists())

    def test_threshold_option_changes_what_is_flagged(self):
        self._run("--apply", "--min-visitors-per-ip", "3")
        self.assertTrue(AnalyticsEvent.objects.filter(ip_hash=self.SMALL_IP, is_suspected_bot=True).exists())
        self.assertFalse(AnalyticsEvent.objects.filter(ip_hash=self.BUSY_IP, is_suspected_bot=True).exists())

    def test_unflag_is_dry_run_until_applied(self):
        self._run("--apply")
        self._run("--unflag")
        self.assertEqual(AnalyticsEvent.objects.filter(is_suspected_bot=True).count(), 16)
        self._run("--unflag", "--apply")
        self.assertFalse(AnalyticsEvent.objects.filter(is_suspected_bot=True).exists())


class AudienceDashboardBotShareTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="admin2@example.com", username="admin2", password="test-password",
            is_superuser=True, is_staff=True,
        )
        self.client.force_authenticate(self.admin)

    def _visit(self, visitor_id, **fields):
        return AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT, visitor_id=visitor_id, **fields
        )

    def _audience(self):
        cache.clear()
        response = self.client.get(reverse("admin-analytics-audience"), {"days": 30})
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_flagged_rows_are_excluded_and_counted(self):
        self._visit("human", country_code="NP", ua_family="chrome_mobile")
        self._visit("bot-a", is_suspected_bot=True, country_code="US", ua_family="other_desktop")
        self._visit("bot-b", is_suspected_bot=True, country_code="US", ua_family="other_desktop")

        data = self._audience()

        self.assertEqual(data["summary"]["visitors"], 1)
        self.assertEqual(data["summary"]["total_page_views"], 1)
        self.assertEqual(data["summary"]["suspected_bot_events"], 2)
        self.assertEqual(data["countries"], [{"country_code": "NP", "visitors": 1}])
        self.assertEqual(data["ua_families"], [{"ua_family": "chrome_mobile", "visitors": 1}])

    def test_countries_and_ua_families_count_distinct_visitors_and_skip_blanks(self):
        self._visit("v1", country_code="NP", ua_family="chrome_mobile")
        self._visit("v1", country_code="NP", ua_family="chrome_mobile")
        self._visit("v2", country_code="NP", ua_family="safari_mobile")
        self._visit("v3", country_code="IN", ua_family="chrome_mobile")
        self._visit("v4")

        data = self._audience()

        self.assertEqual(
            data["countries"],
            [{"country_code": "NP", "visitors": 2}, {"country_code": "IN", "visitors": 1}],
        )
        self.assertEqual(
            data["ua_families"],
            [{"ua_family": "chrome_mobile", "visitors": 2}, {"ua_family": "safari_mobile", "visitors": 1}],
        )

    def test_referral_sources_merge_direct_and_drop_internal(self):
        self._visit("r1")  # key missing
        self._visit("r2", metadata={"referral_source": "direct"})
        self._visit("r3", metadata={"referral_source": " Direct "})
        self._visit("r4", metadata={"referral_source": ""})
        self._visit("r5", metadata={"referral_source": "internal"})
        self._visit("r6", metadata={"referral_source": "Facebook"})
        self._visit("r7", metadata={"referral_source": "facebook"})

        data = self._audience()

        sources = {row["referral_source"]: row["count"] for row in data["referral_sources"]}
        self.assertEqual(sources, {"direct": 4, "facebook": 2})
        self.assertEqual(data["referral_sources"][0]["referral_source"], "direct")

    def test_export_includes_country_and_ua_family_tables(self):
        self._visit("human", country_code="NP", ua_family="chrome_mobile")
        cache.clear()

        response = self.client.get(
            reverse("admin-analytics-export"),
            {"sections": "audience", "file_format": "csv", "days": 30},
        )

        self.assertEqual(response.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        self.assertIn("audience_countries.csv", names)
        self.assertIn("audience_ua_families.csv", names)
        self.assertIn("audience_summary.csv", names)


class AdminOverviewApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="overview-admin@example.com", username="overviewadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.staff = User.objects.create_user(
            email="overview-staff@example.com", username="overviewstaff",
            password="test-password", is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="overview-reader@example.com", username="overviewreader", password="test-password"
        )
        self.url = reverse("admin-overview")
        self.client.force_authenticate(self.admin)

    def _story(self, title, **kwargs):
        slug = title.lower().replace(" ", "-")
        return Story.objects.create(title=title, slug=slug, is_published=True, **kwargs)

    def _event(self, event_type, *, days_ago=0, **kwargs):
        kwargs.setdefault("visitor_id", f"visitor-{uuid4().hex[:8]}")
        event = AnalyticsEvent.objects.create(event_type=event_type, **kwargs)
        if days_ago:
            AnalyticsEvent.objects.filter(pk=event.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return event

    def _get(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_requires_a_superuser(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_and_suspected_bot_activity_is_excluded(self):
        story = self._story("Counted Story")
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="real-visitor")
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="bot-visitor", is_suspected_bot=True)
        self._event(AnalyticsEvent.EVENT_VISIT, user=self.admin)
        self._event(AnalyticsEvent.EVENT_VISIT, user=self.staff)
        self._event(AnalyticsEvent.EVENT_STORY_STARTED, user=self.admin, story=story)
        self._event(AnalyticsEvent.EVENT_STORY_STARTED, user=self.reader, story=story)
        self._event(
            AnalyticsEvent.EVENT_READING_SESSION, user=self.staff, story=story, duration_seconds=600
        )
        self._event(AnalyticsEvent.EVENT_AD_IMPRESSION, is_suspected_bot=True)

        kpis = self._get()["kpis"]
        self.assertEqual(kpis["unique_visitors"]["value"], 1)
        self.assertEqual(kpis["stories_started"]["value"], 1)
        self.assertEqual(kpis["engagement_minutes"]["value"], 0)
        self.assertEqual(kpis["ad_impressions"]["value"], 0)

    def test_visitors_are_counted_once_per_identity(self):
        # One signed-in reader on two browsers is one person; a browser that
        # visits twice is one visitor.
        self._event(AnalyticsEvent.EVENT_VISIT, user=self.reader, visitor_id="laptop")
        self._event(AnalyticsEvent.EVENT_VISIT, user=self.reader, visitor_id="phone")
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="anon")
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="anon")
        self.assertEqual(self._get()["kpis"]["unique_visitors"]["value"], 2)

    def test_previous_period_comparison(self):
        for name in ("a", "b", "c"):
            self._event(AnalyticsEvent.EVENT_VISIT, visitor_id=name)
        for name in ("d", "e"):
            self._event(AnalyticsEvent.EVENT_VISIT, visitor_id=name, days_ago=10)
        # Older than both windows: must be in neither.
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="old", days_ago=40)
        self._event(AnalyticsEvent.EVENT_READING_SESSION, duration_seconds=1200)
        self._event(AnalyticsEvent.EVENT_LISTENING_SESSION, duration_seconds=600, days_ago=9)

        kpis = self._get(days=7)["kpis"]
        visitors = kpis["unique_visitors"]
        self.assertEqual((visitors["value"], visitors["previous_value"]), (3, 2))
        self.assertEqual(visitors["change_pct"], 50.0)
        minutes = kpis["engagement_minutes"]
        self.assertEqual((minutes["value"], minutes["previous_value"]), (20.0, 10.0))
        self.assertEqual(minutes["change_pct"], 100.0)
        # The sparkline has one entry per day bucket and adds up to the visits.
        self.assertEqual(len(visitors["sparkline"]), 8)
        self.assertEqual(sum(visitors["sparkline"]), 3)

    def test_change_is_null_when_the_previous_value_is_zero(self):
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="only-now")
        visitors = self._get(days=7)["kpis"]["unique_visitors"]
        self.assertEqual((visitors["value"], visitors["previous_value"]), (1, 0))
        self.assertIsNone(visitors["change_pct"])

    def test_new_signups_exclude_staff(self):
        # setUp's admin, staff and reader all joined just now; only the reader counts.
        self.assertEqual(self._get(days=7)["kpis"]["new_signups"]["value"], 1)

    def test_days_param_is_validated(self):
        self.assertEqual(self._get()["range_days"], 7)
        self.assertEqual(self._get(days=1)["range_days"], 1)
        self.assertEqual(self._get(days=30)["range_days"], 30)
        self.assertEqual(self._get(days=90)["range_days"], 90)
        self.assertEqual(self._get(days=3)["range_days"], 7)
        self.assertEqual(self._get(days="abc")["range_days"], 7)
        self.assertEqual(self._get(days="-7")["range_days"], 7)
        self.assertEqual(self._get(days=1)["time_interval"], "hour")
        everything = self._get(days="all")
        self.assertEqual(everything["range_days"], "all")
        self.assertIsNone(everything["kpis"]["unique_visitors"]["previous_value"])

    def test_response_is_cached_per_range(self):
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="first")
        self.assertEqual(self._get(days=7)["kpis"]["unique_visitors"]["value"], 1)
        self._event(AnalyticsEvent.EVENT_VISIT, visitor_id="second")
        self.assertEqual(self._get(days=7)["kpis"]["unique_visitors"]["value"], 1)
        self.assertEqual(self._get(days=30)["kpis"]["unique_visitors"]["value"], 2)

    def test_funnel_steps_and_rates(self):
        story = self._story("Funnel Story")
        for index in range(4):
            self._event(AnalyticsEvent.EVENT_VISIT, visitor_id=f"v{index}")
        for _ in range(2):
            StoryView.objects.create(story=story, ip_address="198.51.100.7")
        self._event(AnalyticsEvent.EVENT_STORY_STARTED, story=story)
        self._event(AnalyticsEvent.EVENT_STORY_COMPLETED, story=story)

        funnel = {step["key"]: step for step in self._get()["funnel"]}
        self.assertEqual(
            [funnel[key]["count"] for key in ("visits", "story_opened", "started", "completed")],
            [4, 2, 1, 1],
        )
        self.assertIsNone(funnel["visits"]["rate"])
        self.assertEqual(funnel["story_opened"]["rate"], 0.5)
        self.assertEqual(funnel["started"]["rate"], 0.5)
        self.assertEqual(funnel["completed"]["rate"], 1.0)

    def test_listening_is_grouped_by_story_not_by_audio_track(self):
        story = self._story("Audiobook")
        tracks = [Audio.objects.create(story=story, title=f"Part {n}", slug=f"part-{n}", order=n + 1) for n in range(3)]
        for track, progress in zip(tracks, (1.0, 0.0, 0.0)):
            AudioReadingProgress.objects.create(
                user=self.reader, story=story, audio=track, progress=progress
            )
            self._event(
                AnalyticsEvent.EVENT_LISTENING_SESSION,
                user=self.reader, story=story, duration_seconds=300,
            )

        data = self._get()
        listen = data["formats"]["listen"]
        # One track finished out of three is a third of the book, not 100%.
        self.assertEqual(listen["avg_completion"], 0.333)
        self.assertEqual(listen["unique_users"], 1)
        self.assertEqual(listen["minutes"], 15.0)
        # No per-track lists any more.
        self.assertNotIn("most_listened_audios", data)

    def test_format_completion_ignores_staff_progress(self):
        story = self._story("Read Story")
        chapter = Chapter.objects.create(story=story, title="One", content="<p>x</p>", order=1)
        ChapterReadingProgress.objects.create(user=self.reader, story=story, chapter=chapter, progress=0.5)
        ChapterReadingProgress.objects.create(user=self.admin, story=story, chapter=chapter, progress=1.0)
        self.assertEqual(self._get()["formats"]["read"]["avg_completion"], 0.5)

    def test_trending_ranks_and_rank_change(self):
        alpha, bravo, charlie = (self._story(name) for name in ("Alpha", "Bravo", "Charlie"))
        for _ in range(3):
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=alpha)
        self._event(AnalyticsEvent.EVENT_READING_SESSION, story=alpha, days_ago=10)
        self._event(AnalyticsEvent.EVENT_READING_SESSION, story=bravo)
        for _ in range(2):
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=bravo, days_ago=10)
        self._event(AnalyticsEvent.EVENT_READING_SESSION, story=charlie)

        trending = self._get(days=7)["movers"]["trending"]
        self.assertEqual([row["title"] for row in trending], ["Alpha", "Bravo", "Charlie"])
        # Alpha was 2nd last week and Bravo 1st; Charlie wasn't ranked at all.
        self.assertEqual([row["rank_change"] for row in trending], [1, -1, None])

    def test_hidden_gems_and_leaky_stories(self):
        gem, leak, popular = (self._story(name) for name in ("Gem", "Leak", "Popular"))
        for story in (leak, popular):
            for _ in range(5):
                StoryView.objects.create(story=story, ip_address="198.51.100.9")
        for _ in range(4):
            self._event(AnalyticsEvent.EVENT_STORY_STARTED, story=gem)
        for _ in range(3):
            self._event(AnalyticsEvent.EVENT_STORY_COMPLETED, story=gem)
        for _ in range(6):
            self._event(AnalyticsEvent.EVENT_STORY_STARTED, story=leak)
        self._event(AnalyticsEvent.EVENT_STORY_COMPLETED, story=leak)
        # Too few starts to say anything about.
        thin = self._story("Thin")
        self._event(AnalyticsEvent.EVENT_STORY_STARTED, story=thin)
        self._event(AnalyticsEvent.EVENT_STORY_COMPLETED, story=thin)

        movers = self._get()["movers"]
        self.assertEqual([row["title"] for row in movers["hidden_gems"]], ["Gem"])
        self.assertEqual(movers["hidden_gems"][0]["completion_rate"], 0.75)
        self.assertEqual([row["title"] for row in movers["leaky"]], ["Leak"])

    def test_sources_and_publish_markers(self):
        story = self._story("Fresh Story", site_published_date=timezone.localdate())
        self._event(AnalyticsEvent.EVENT_VISIT, country_code="NP", metadata={"referral_source": "google"})
        self._event(AnalyticsEvent.EVENT_VISIT, country_code="NP", metadata={"referral_source": "google"})
        self._event(AnalyticsEvent.EVENT_VISIT, country_code="US", metadata={})

        data = self._get(days=7)
        sources = data["sources"]
        self.assertEqual(sources["referrers"][0], {"referral_source": "google", "count": 2})
        self.assertEqual(sources["countries"][0], {"country_code": "NP", "visitors": 2})
        markers = data["traffic"]["publish_markers"]
        self.assertEqual([m["title"] for m in markers], [story.title])
        self.assertIsNotNone(markers[0]["bucket"])
        # The 24h chart is hourly, so a publish date has nowhere to sit.
        self.assertIsNone(self._get(days=1)["traffic"]["publish_markers"][0]["bucket"])

    def test_catalog_totals(self):
        story = self._story("Catalogued")
        Chapter.objects.create(story=story, title="One", content="<p>x</p>", order=1)
        catalog = self._get()["catalog"]
        self.assertEqual(catalog["stories"], 1)
        self.assertEqual(catalog["chapters"], 1)
        self.assertEqual(catalog["users"], 3)


class AdminOverviewNeedsAttentionTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="attention-admin@example.com", username="attentionadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.client.force_authenticate(self.admin)
        self.url = reverse("admin-overview")

    def _items(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        return {item["key"]: item for item in response.data["needs_attention"]}

    def test_nothing_to_flag_returns_an_empty_list(self):
        self.assertEqual(self._items(), {})

    def test_zero_count_items_are_left_out(self):
        Submission.objects.create(user=self.admin, title="Pending", about="a", content="<p>x</p>")
        items = self._items()
        self.assertEqual(list(items), ["submissions"])
        self.assertEqual(items["submissions"]["count"], 1)
        self.assertEqual(items["submissions"]["link"], "/admin/submissions")

    def test_each_attention_item_is_counted_and_linked(self):
        from apps.feedback.models import Feedback
        from apps.story.models import BookFetchJob, EpubImportJob, StoryQueue

        Feedback.objects.create(message="Something is broken")
        Feedback.objects.create(message="Already handled", status=Feedback.STATUS_RESOLVED)

        # Live, with a cover and audio: flags nothing by itself.
        complete = Story.objects.create(
            title="Complete", slug="complete", is_published=True, cover_image="https://example.com/c.jpg"
        )
        Audio.objects.create(story=complete, title="Track one", slug="track-one")
        Audio.objects.create(story=complete, title="Track two", slug="track-two", order=2)
        # Live, but no cover and no audio.
        Story.objects.create(title="Bare", slug="bare", is_published=True)
        # A summary and a retrospective that both failed: still one story.
        Story.objects.create(
            title="Failed AI", slug="failed-ai", is_published=False, cover_image="https://example.com/f.jpg",
            summary_status=Story.GEN_STATUS_FAILED, retrospective_status=Story.GEN_STATUS_FAILED,
        )
        # Going live in 3 days counts; in 30 days doesn't.
        Story.objects.create(
            title="Scheduled", slug="scheduled", is_published=True,
            publish_at=timezone.now() + timedelta(days=3), cover_image="https://example.com/s.jpg",
        )
        Story.objects.create(
            title="Later", slug="later", is_published=True,
            publish_at=timezone.now() + timedelta(days=30), cover_image="https://example.com/l.jpg",
        )
        EpubImportJob.objects.create(story=complete, status=EpubImportJob.STATUS_FAILED)
        EpubImportJob.objects.create(story=complete, status=EpubImportJob.STATUS_COMPLETED)
        BookFetchJob.objects.create(requested_count=5, status=BookFetchJob.STATUS_FAILED)
        old_failure = BookFetchJob.objects.create(requested_count=5, status=BookFetchJob.STATUS_FAILED)
        BookFetchJob.objects.filter(pk=old_failure.pk).update(created_at=timezone.now() - timedelta(days=90))
        StoryQueue.objects.create(title="Waiting")
        StoryQueue.objects.create(title="Done", is_added=True)

        items = self._items()
        self.assertEqual(
            {key: item["count"] for key, item in items.items()},
            {
                "feedback": 1,
                "failed_ai": 1,
                "failed_epub_imports": 1,
                "failed_book_fetches": 1,
                "scheduled": 1,
                "missing_cover": 1,
                "missing_audio": 1,
                "queue_backlog": 1,
            },
        )
        self.assertNotIn("submissions", items)
        self.assertEqual(items["feedback"]["link"], "/admin/feedback")


class SessionCapTests(APITestCase):
    """The one helper that decides how much of a session counts as time spent."""

    def setUp(self):
        from apps.story.content_scoring import SessionCaps

        self.SessionCaps = SessionCaps
        self.story = Story.objects.create(title="Capped", slug="capped", is_published=True)

    def _caps(self):
        return self.SessionCaps()

    def test_reading_is_capped_at_one_and_a_half_times_the_estimated_reading_time(self):
        Story.objects.filter(pk=self.story.pk).update(cached_chapter_reading_minutes=10)
        caps = self._caps()
        reading = AnalyticsEvent.EVENT_READING_SESSION
        self.assertEqual(caps.capped(10 * 3600, reading, story_id=self.story.id), 15 * 60)
        self.assertEqual(caps.capped(120, reading, story_id=self.story.id), 120)

    def test_listening_and_watching_use_media_length(self):
        Audio.objects.create(
            story=self.story, title="One", slug="one", order=1, audio_file="story_audios/a.mp3",
            duration_seconds=1200,
        )
        Audio.objects.create(
            story=self.story, title="Two", slug="two", order=2, audio_file="story_audios/b.mp3",
            duration_seconds=600,
        )
        video = Video.objects.create(
            story=self.story, title="V", slug="v", order=1, youtube_url="https://youtu.be/abcdefghijk",
            youtube_id="abcdefghijk", duration_seconds=1200,
        )
        caps = self._caps()
        # Both tracks together are 30 minutes of audio: 1.5x = 45 minutes.
        self.assertEqual(caps.capped(10 * 3600, AnalyticsEvent.EVENT_LISTENING_SESSION, story_id=self.story.id), 45 * 60)
        self.assertEqual(
            caps.capped(10 * 3600, AnalyticsEvent.EVENT_WATCHING_SESSION, story_id=self.story.id, video_id=video.id),
            30 * 60,
        )

    def test_fallbacks_floor_and_ceiling(self):
        caps = self._caps()
        reading = AnalyticsEvent.EVENT_READING_SESSION
        # Length unknown -> 60 minutes; a blog post is never known.
        self.assertEqual(caps.capped(5 * 3600, reading, story_id=self.story.id), 60 * 60)
        self.assertEqual(caps.capped(5 * 3600, reading, blog_id=1), 60 * 60)
        # A Quick Read is a couple of minutes long.
        self.assertEqual(caps.capped(5 * 3600, reading, story_id=self.story.id, is_quick_read=True), 10 * 60)
        # A very short story is still allowed 10 minutes; a 20h audiobook is not one sitting.
        Story.objects.filter(pk=self.story.pk).update(cached_chapter_reading_minutes=2)
        Audio.objects.create(
            story=self.story, title="Long", slug="long", order=1, audio_file="story_audios/l.mp3",
            duration_seconds=20 * 3600,
        )
        caps = self._caps()
        self.assertEqual(caps.capped(5 * 3600, reading, story_id=self.story.id), 10 * 60)
        self.assertEqual(
            caps.capped(20 * 3600, AnalyticsEvent.EVENT_LISTENING_SESSION, story_id=self.story.id), 3 * 3600
        )


class AdminContentTabTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="content-admin@example.com", username="contentadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.staff = User.objects.create_user(
            email="content-staff@example.com", username="contentstaff",
            password="test-password", is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="content-reader@example.com", username="contentreader", password="test-password"
        )
        self.url = reverse("admin-analytics-content")
        self.client.force_authenticate(self.admin)

    def _story(self, title, **kwargs):
        return Story.objects.create(
            title=title, slug=title.lower().replace(" ", "-"), is_published=True, **kwargs
        )

    def _event(self, event_type, *, days_ago=0, **kwargs):
        kwargs.setdefault("visitor_id", f"visitor-{uuid4().hex[:8]}")
        event = AnalyticsEvent.objects.create(event_type=event_type, **kwargs)
        if days_ago:
            AnalyticsEvent.objects.filter(pk=event.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return event

    def _read(self, story, *, seconds=300, days_ago=0, **kwargs):
        return self._event(
            AnalyticsEvent.EVENT_READING_SESSION, story=story, duration_seconds=seconds, days_ago=days_ago, **kwargs
        )

    def _get(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def _rows(self, data, kind="story"):
        return {row["title"]: row for row in data["top_content"][kind]["rows"]}

    # --- minutes ------------------------------------------------------------

    def test_idle_tab_sessions_are_capped_in_rows_and_kpis(self):
        story = self._story("Idle Story")
        blog = Blog.objects.create(title="Idle Blog", slug="idle-blog", content="<p>x</p>")
        self._read(story, seconds=20 * 3600)
        self._event(AnalyticsEvent.EVENT_READING_SESSION, blog=blog, duration_seconds=1083 * 60)

        data = self._get(days=7)
        # An unknown-length story and every blog post are capped at 60 minutes.
        self.assertEqual(self._rows(data)["Idle Story"]["reading_minutes"], 60.0)
        self.assertEqual(self._rows(data, "blog")["Idle Blog"]["engagement_minutes"], 60.0)
        self.assertEqual(data["kpis"]["engagement_minutes"]["value"], 120.0)

    # --- scoring ------------------------------------------------------------

    def test_scores_are_normalised_within_each_content_type(self):
        big, small = self._story("Big Story"), self._story("Small Story")
        for _ in range(2):
            self._read(big, seconds=600)
        self._read(small, seconds=300)
        blog_big = Blog.objects.create(title="Big Blog", slug="big-blog", content="<p>x</p>")
        blog_small = Blog.objects.create(title="Small Blog", slug="small-blog", content="<p>x</p>")
        for _ in range(2):
            self._event(AnalyticsEvent.EVENT_READING_SESSION, blog=blog_big, duration_seconds=600)
        self._event(AnalyticsEvent.EVENT_READING_SESSION, blog=blog_small, duration_seconds=300)

        data = self._get(days=7)
        stories, blogs = self._rows(data), self._rows(data, "blog")
        # The best of each type scores the same: 35 (people) + 25 (time), nothing else.
        self.assertAlmostEqual(stories["Big Story"]["performance_score"], 60.0, delta=0.1)
        self.assertAlmostEqual(blogs["Big Blog"]["performance_score"], 60.0, delta=0.1)
        # Half the people, a quarter of the time: 100 x (.35 x .5 + .25 x .25).
        self.assertAlmostEqual(stories["Small Story"]["performance_score"], 23.8, delta=0.1)
        for row in list(stories.values()) + list(blogs.values()):
            self.assertTrue(0 <= row["performance_score"] <= 100)
        self.assertIn("weights", data["score_formula"])

    def test_one_long_session_does_not_outrank_a_story_many_people_read(self):
        popular, lone = self._story("Popular"), self._story("Lone Reader")
        for _ in range(4):
            self._read(popular, seconds=240)
        self._read(lone, seconds=3000)

        rows = self._get(days=7)["top_content"]["story"]["rows"]
        self.assertEqual([row["title"] for row in rows][:2], ["Popular", "Lone Reader"])
        self.assertFalse(rows[0]["low_sample"])
        self.assertTrue(rows[1]["low_sample"])

    def test_low_sample_rows_sort_last_whatever_their_score(self):
        two_people, one_person = self._story("Two People"), self._story("One Person")
        self._read(two_people, seconds=60)
        self._read(two_people, seconds=60)
        self._read(one_person, seconds=3000)
        for _ in range(3):
            self._event(AnalyticsEvent.EVENT_STORY_STARTED, user=self.reader, story=one_person)
        user = User.objects.create_user(email="done@example.com", username="done", password="x")
        self._event(AnalyticsEvent.EVENT_STORY_COMPLETED, user=user, story=one_person)

        by_score = self.client.get(
            reverse("admin-analytics-content-rankings"), {"kind": "story", "days": 7}
        ).data["results"]
        self.assertEqual([row["title"] for row in by_score], ["Two People", "One Person"])
        by_minutes = self.client.get(
            reverse("admin-analytics-content-rankings"),
            {"kind": "story", "days": 7, "sort": "engagement_minutes"},
        ).data["results"]
        self.assertEqual([row["title"] for row in by_minutes][:1], ["Two People"])

    def test_audiobook_with_no_listens_scores_zero(self):
        audiobook = self._story("Silent Audiobook")
        Audio.objects.create(
            story=audiobook, title="One", slug="one", order=1, audio_file="story_audios/s.mp3"
        )
        StoryView.objects.create(story=audiobook, ip_address="198.51.100.20")
        Favorite.objects.create(story=audiobook, user=self.reader)
        self._read(audiobook)  # reading the text is not listening

        row = self._rows(self._get(days=7), "audiobook")["Silent Audiobook"]
        self.assertEqual(row["unique_people"], 0)
        self.assertEqual(row["performance_score"], 0)
        self.assertTrue(row["low_sample"])

    def test_audiobooks_are_scored_on_listening_only(self):
        audiobook = self._story("Heard Audiobook")
        Audio.objects.create(
            story=audiobook, title="One", slug="one", order=1, audio_file="story_audios/h.mp3"
        )
        self._read(audiobook, seconds=900)
        for _ in range(2):
            self._event(AnalyticsEvent.EVENT_LISTENING_SESSION, story=audiobook, duration_seconds=300)

        row = self._rows(self._get(days=7), "audiobook")["Heard Audiobook"]
        self.assertEqual(row["unique_people"], 2)
        self.assertEqual(row["engagement_minutes"], 10.0)  # listening only, not the 15 reading minutes

    # --- views and reads ----------------------------------------------------

    def test_views_are_never_fewer_than_the_people_who_read(self):
        """Readers who arrive on a reader page directly never fire the detail-page
        view beacon, so a story could show 0 views and 8 reads."""
        story = self._story("Direct Readers")
        visitors = ["a", "b", "c"]  # 6 sessions, 3 people
        for visitor in visitors:
            self._read(story, visitor_id=visitor)
            self._read(story, visitor_id=visitor)

        row = self._rows(self._get(days=7))["Direct Readers"]
        self.assertEqual(row["reads"], 6)
        self.assertEqual(row["unique_people"], 3)
        self.assertEqual(row["views"], 3)

    def test_detail_page_views_still_count_when_nobody_reads(self):
        story = self._story("Browsed Only")
        for index in range(4):
            StoryView.objects.create(story=story, ip_address=f"198.51.100.{index + 30}")
        data = self._get(days=7)
        self.assertEqual(self._rows(data)["Browsed Only"]["views"], 4)
        self.assertEqual(data["kpis"]["views"]["value"], 4)
        self.assertEqual(data["views_over_time"]["total"], 4)
        self.assertEqual(sum(row["count"] for row in data["views_over_time"]["series"]), 4)

    # --- every section respects days ----------------------------------------

    def test_every_section_respects_the_selected_range(self):
        genre = Genre.objects.create(name="Folk Tales")
        story = self._story("Old Hit")
        story.genres.add(genre)
        Story.objects.filter(pk=story.pk).update(site_published_date=timezone.localdate() - timedelta(days=40))
        old_blog = Blog.objects.create(title="Old Blog", slug="old-blog", content="<p>x</p>")
        Blog.objects.filter(pk=old_blog.pk).update(created_at=timezone.now() - timedelta(days=40))
        for index in range(3):
            self._read(story, days_ago=40, visitor_id=f"old-{index}")
        view = StoryView.objects.create(story=story, ip_address="198.51.100.50")
        StoryView.objects.filter(pk=view.pk).update(created_at=timezone.now() - timedelta(days=40))
        Favorite.objects.create(story=story, user=self.reader)
        Favorite.objects.filter(story=story).update(created_at=timezone.now() - timedelta(days=40))

        recent = self._get(days=7)
        genre_row = next(row for row in recent["genre_performance"] if row["name"] == "Folk Tales")
        self.assertEqual(genre_row["stories_in_catalogue"], 1)
        self.assertEqual(
            (genre_row["stories_with_activity"], genre_row["views"], genre_row["unique_readers"], genre_row["engagement_minutes"]),
            (0, 0, 0, 0),
        )
        story_row = self._rows(recent)["Old Hit"]
        self.assertEqual((story_row["views"], story_row["unique_people"], story_row["favorites"]), (0, 0, 0))
        type_row = recent["story_type_performance"][0]
        self.assertEqual((type_row["views"], type_row["unique_readers"]), (0, 0))
        self.assertEqual(recent["catalogue_gaps"]["no_audio"]["rows"][0]["views"], 0)
        self.assertEqual(recent["publishing"]["stories"], 0)
        self.assertEqual(recent["publishing"]["blogs"], 0)
        self.assertEqual(recent["kpis"]["views"]["value"], 0)
        self.assertEqual(recent["kpis"]["unique_readers"]["value"], 0)
        self.assertEqual(recent["views_over_time"]["publish_markers"], [])

        wide = self._get(days=90)
        genre_row = next(row for row in wide["genre_performance"] if row["name"] == "Folk Tales")
        self.assertEqual((genre_row["views"], genre_row["unique_readers"]), (3, 3))
        self.assertEqual(wide["publishing"]["stories"], 1)
        self.assertEqual(wide["publishing"]["blogs"], 1)
        self.assertEqual(wide["kpis"]["titles_published"]["value"], 2)
        self.assertEqual(self._rows(wide)["Old Hit"]["favorites"], 1)

    # --- exclusions ---------------------------------------------------------

    def test_staff_and_suspected_bot_activity_is_excluded(self):
        story = self._story("Watched Story")
        self._read(story, visitor_id="real")
        self._read(story, user=self.admin, seconds=3000)
        self._read(story, user=self.staff, seconds=3000)
        self._read(story, visitor_id="bot", is_suspected_bot=True, seconds=3000)
        StoryView.objects.create(story=story, user=self.admin, ip_address="198.51.100.60")
        Favorite.objects.create(story=story, user=self.staff)

        data = self._get(days=7)
        row = self._rows(data)["Watched Story"]
        self.assertEqual((row["unique_people"], row["views"], row["favorites"], row["reads"]), (1, 1, 0, 1))
        self.assertEqual(row["reading_minutes"], 5.0)
        self.assertEqual(data["kpis"]["unique_readers"]["value"], 1)
        self.assertEqual(data["kpis"]["engagement_minutes"]["value"], 5.0)

    # --- previous period ----------------------------------------------------

    def test_kpis_compare_with_the_previous_period_only(self):
        story = self._story("Trendy")
        for index in range(3):
            self._read(story, visitor_id=f"now-{index}")
        self._read(story, visitor_id="before", days_ago=10)

        kpis = self._get(days=7)["kpis"]
        readers = kpis["unique_readers"]
        # Previous is the week before, not "the last 14 days".
        self.assertEqual((readers["value"], readers["previous_value"]), (3, 1))
        self.assertEqual(readers["change_pct"], 200.0)

    def test_change_is_null_when_nothing_happened_in_the_previous_period(self):
        story = self._story("Brand New")
        self._read(story)
        kpis = self._get(days=7)["kpis"]
        self.assertEqual(kpis["unique_readers"]["previous_value"], 0)
        self.assertIsNone(kpis["unique_readers"]["change_pct"])
        self.assertIsNone(self._get(days="all")["kpis"]["unique_readers"]["previous_value"])

    def test_rows_carry_previous_score_and_rank_change(self):
        alpha, bravo = self._story("Alpha"), self._story("Bravo")
        for index in range(3):
            self._read(alpha, visitor_id=f"a{index}")
        self._read(bravo, visitor_id="b0")
        self._read(bravo, visitor_id="b1")
        # Last week the order was the other way round.
        for index in range(3):
            self._read(bravo, visitor_id=f"pb{index}", days_ago=10)
        self._read(alpha, visitor_id="pa0", days_ago=10)
        self._read(alpha, visitor_id="pa1", days_ago=10)

        rows = self._rows(self._get(days=7))
        self.assertEqual(rows["Alpha"]["rank_change"], 1)
        self.assertEqual(rows["Bravo"]["rank_change"], -1)
        self.assertGreater(rows["Bravo"]["previous_score"], rows["Alpha"]["previous_score"])
        self.assertIsNone(self._rows(self._get(days="all"))["Alpha"]["rank_change"])

    def test_views_over_time_keeps_a_previous_series(self):
        story = self._story("Charted")
        for index in range(2):
            StoryView.objects.create(story=story, ip_address=f"198.51.100.{index + 70}")
        old = StoryView.objects.create(story=story, ip_address="198.51.100.80")
        StoryView.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))

        chart = self._get(days=7)["views_over_time"]
        self.assertEqual((chart["total"], chart["previous_total"]), (2, 1))
        self.assertEqual(chart["change_pct"], 100.0)
        self.assertEqual(len(chart["previous_series"]), 8)
        self.assertEqual(sum(row["count"] for row in chart["previous_series"]), 1)
        self.assertEqual(sum(row["count"] for row in chart["series"]), 2)

    # --- genres, types, gaps, publishing ------------------------------------

    def test_genre_and_story_type_performance_are_in_range_and_sorted_by_views(self):
        quiet, busy = Genre.objects.create(name="Quiet"), Genre.objects.create(name="Busy")
        Genre.objects.create(name="Empty Genre")
        a, b = self._story("Quiet Story"), self._story("Busy Story")
        a.genres.add(quiet)
        b.genres.add(busy)
        self._read(a)
        for index in range(3):
            self._read(b, visitor_id=f"busy-{index}", seconds=600)

        data = self._get(days=7)
        genres = data["genre_performance"]
        self.assertEqual([row["name"] for row in genres], ["Busy", "Quiet"])  # no genre without stories
        busy_row = genres[0]
        self.assertEqual(busy_row["views"], 3)
        self.assertEqual(busy_row["unique_readers"], 3)
        self.assertEqual(busy_row["engagement_minutes"], 30.0)
        self.assertEqual(busy_row["views_per_story"], 3.0)
        self.assertIsNone(busy_row["completion_rate"])
        self.assertEqual(sum(row["views"] for row in data["story_type_performance"]), 4)

    def test_completion_rate_is_the_share_of_signed_in_readers_who_finished(self):
        story = self._story("Finishable")
        finisher = User.objects.create_user(email="fin@example.com", username="fin", password="x")
        for user in (self.reader, finisher):
            self._event(AnalyticsEvent.EVENT_STORY_STARTED, user=user, story=story)
        self._event(AnalyticsEvent.EVENT_STORY_COMPLETED, user=finisher, story=story)

        row = self._rows(self._get(days=7))["Finishable"]
        self.assertEqual(row["completion_rate"], 0.5)
        self.assertEqual(row["completions"], 1)

    def test_catalogue_gaps_rank_by_views_in_range(self):
        complete = self._story("Complete", cover_image="https://example.com/c.jpg", summary="<p>s</p>")
        Audio.objects.create(story=complete, title="T", slug="t", order=1, audio_file="story_audios/c.mp3")
        Video.objects.create(
            story=complete, title="V", slug="v", order=1, youtube_url="https://youtu.be/abcdefghijk",
            youtube_id="abcdefghijk",
        )
        popular, ignored = self._story("Popular Gap"), self._story("Ignored Gap")
        for index in range(3):
            StoryView.objects.create(story=popular, ip_address=f"198.51.100.{index + 90}")
        old = StoryView.objects.create(story=ignored, ip_address="198.51.100.99")
        StoryView.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=60))

        gaps = self._get(days=7)["catalogue_gaps"]
        self.assertEqual(gaps["no_audio"]["total"], 2)
        self.assertEqual([row["title"] for row in gaps["no_audio"]["rows"]], ["Popular Gap", "Ignored Gap"])
        self.assertEqual(gaps["no_audio"]["rows"][0]["views"], 3)
        self.assertEqual(gaps["no_audio"]["rows"][1]["views"], 0)
        self.assertEqual(set(gaps["no_audio"]["rows"][0]["missing"]), {"audio", "quick_read", "cover", "video"})
        self.assertNotIn("Complete", [row["title"] for key in gaps for row in gaps[key]["rows"]])

    def test_publishing_counts_and_markers(self):
        story = self._story("Fresh Story")
        blog = Blog.objects.create(title="Fresh Blog", slug="fresh-blog", content="<p>x</p>")

        data = self._get(days=7)
        self.assertEqual((data["publishing"]["stories"], data["publishing"]["blogs"]), (1, 1))
        self.assertEqual(sum(row["stories"] for row in data["publishing"]["series"]), 1)
        self.assertEqual(sum(row["blogs"] for row in data["publishing"]["series"]), 1)
        markers = {marker["title"]: marker for marker in data["views_over_time"]["publish_markers"]}
        self.assertEqual(set(markers), {story.title, blog.title})
        self.assertEqual(markers["Fresh Blog"]["type"], "blog")
        self.assertIsNotNone(markers["Fresh Story"]["bucket"])

    def test_content_export_still_works(self):
        story = self._story("Exported")
        self._read(story)
        for file_format in ("csv", "xlsx"):
            response = self.client.get(
                reverse("admin-analytics-export"), {"sections": "content", "file_format": file_format, "days": 7}
            )
            self.assertEqual(response.status_code, 200, file_format)


class AdminEngagementTabTests(APITestCase):
    """Engagement is computed from AnalyticsEvent so anonymous readers count."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="engagement-admin@example.com", username="engagementadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.staff = User.objects.create_user(
            email="engagement-staff@example.com", username="engagementstaff",
            password="test-password", is_staff=True, is_active=True,
        )
        self.url = reverse("admin-analytics-engagement")
        self.client.force_authenticate(self.admin)
        self.counter = 0

    def _story(self, title, **kwargs):
        return Story.objects.create(
            title=title, slug=title.lower().replace(" ", "-"), is_published=True, **kwargs
        )

    def _user(self):
        self.counter += 1
        return User.objects.create_user(
            email=f"eng{self.counter}@example.com", username=f"eng{self.counter}", password="x"
        )

    def _event(self, event_type, *, days_ago=0, **kwargs):
        kwargs.setdefault("visitor_id", f"visitor-{uuid4().hex[:8]}")
        event = AnalyticsEvent.objects.create(event_type=event_type, **kwargs)
        if days_ago:
            AnalyticsEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return event

    def _start(self, story, *, visitor_id=None, user=None, milestones=(), fmt="chapter", days_ago=0, resumed=False):
        """One reader opens a story and crosses the given progress milestones."""
        who = {"user": user, "visitor_id": f"u{user.pk}" if user else (visitor_id or f"v{uuid4().hex[:8]}")}
        self._event(
            AnalyticsEvent.EVENT_STORY_RESUMED if resumed else AnalyticsEvent.EVENT_STORY_STARTED,
            story=story, metadata={"format": fmt}, days_ago=days_ago, **who,
        )
        for milestone in milestones:
            self._event(
                AnalyticsEvent.EVENT_STORY_PROGRESSED, story=story, value=milestone,
                metadata={"format": fmt, "milestone": milestone}, days_ago=days_ago, **who,
            )
        return who

    def _finish(self, story, user, source="chapters", days_ago=0):
        self._event(
            AnalyticsEvent.EVENT_STORY_COMPLETED, story=story, user=user, visitor_id="server",
            metadata={"source": source}, days_ago=days_ago,
        )

    def _view(self, story, count, days_ago=0):
        for index in range(count):
            view = StoryView.objects.create(story=story, ip_address=f"198.51.100.{index + 1}")
            if days_ago:
                StoryView.objects.filter(pk=view.pk).update(created_at=timezone.now() - timedelta(days=days_ago))

    def _get(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    # --- the root problem -----------------------------------------------------

    def test_anonymous_readers_count(self):
        story = self._story("Anon Story")
        self._view(story, 8)
        for index in range(6):
            self._start(story, visitor_id=f"anon-{index}")

        kpis = self._get(days=7)["kpis"]
        # Nothing here is signed in, and previously this was 0% with 0 readers.
        self.assertEqual(kpis["start_rate"]["sample_size"], 8)
        self.assertEqual(kpis["start_rate"]["value"], 0.75)

    def test_start_rate_never_exceeds_100_percent_when_readers_skip_the_detail_page(self):
        story = self._story("Direct Entry")
        self._view(story, 1)
        for index in range(6):
            self._start(story, visitor_id=f"direct-{index}")
        kpi = self._get(days=7)["kpis"]["start_rate"]
        self.assertEqual((kpi["value"], kpi["sample_size"]), (1.0, 6))

    def test_staff_and_suspected_bots_are_excluded(self):
        story = self._story("Counted")
        for index in range(5):
            self._start(story, visitor_id=f"real-{index}")
        self._start(story, user=self.staff, milestones=(0.5,))
        self._start(story, user=self.admin)
        self._event(
            AnalyticsEvent.EVENT_STORY_STARTED, story=story, visitor_id="bot",
            metadata={"format": "chapter"}, is_suspected_bot=True,
        )
        self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, user=self.staff, duration_seconds=3000)

        data = self._get(days=7)
        self.assertEqual(data["funnels"]["read"][1]["count"], 5)
        self.assertEqual(data["kpis"]["minutes_per_reader"]["sample_size"], 0)

    # --- minimum sample -------------------------------------------------------

    def test_rates_are_null_below_the_minimum_sample(self):
        story = self._story("Small Sample")
        for index in range(4):
            self._start(story, visitor_id=f"s{index}")
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, duration_seconds=300, visitor_id=f"s{index}")
        kpis = self._get(days=7)["kpis"]
        self.assertIsNone(kpis["minutes_per_reader"]["value"])
        self.assertEqual(kpis["minutes_per_reader"]["sample_size"], 4)

        self._start(story, visitor_id="s4")
        self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, duration_seconds=300, visitor_id="s4")
        cache.clear()
        self.assertEqual(self._get(days=7)["kpis"]["minutes_per_reader"]["value"], 5.0)

    def test_every_kpi_carries_its_sample_and_label(self):
        kpis = self._get(days=7)["kpis"]
        self.assertEqual(
            set(kpis),
            {"start_rate", "completion_rate", "minutes_per_reader", "returning_rate", "audio_listen_through", "video_watch_through"},
        )
        for kpi in kpis.values():
            self.assertIsNone(kpi["value"])
            self.assertEqual(kpi["sample_size"], 0)
            self.assertEqual(kpi["min_sample"], 5)
            self.assertTrue(kpi["sample_label"])

    # --- completion is a share of signed-in readers ----------------------------

    def test_completion_rate_is_over_signed_in_starters_only(self):
        story = self._story("Finishable")
        for index in range(20):  # anonymous readers can't be seen finishing
            self._start(story, visitor_id=f"anon-{index}")
        finishers = [self._user() for _ in range(2)]
        stayers = [self._user() for _ in range(3)]
        for user in finishers + stayers:
            self._start(story, user=user, milestones=(0.5,))
        for user in finishers:
            self._finish(story, user)

        kpi = self._get(days=7)["kpis"]["completion_rate"]
        self.assertEqual(kpi["sample_size"], 5)
        self.assertEqual(kpi["value"], 0.4)
        self.assertEqual(kpi["sample_label"], "signed-in starters")

    # --- funnels -----------------------------------------------------------------

    def test_funnel_steps_never_increase_and_signed_in_steps_use_their_own_base(self):
        story = self._story("Funnelled")
        self._view(story, 15)
        for index in range(10):
            self._start(story, visitor_id=f"f{index}", milestones=(0.25, 0.5) if index < 6 else ())
        users = [self._user() for _ in range(4)]
        for user in users:
            self._start(story, user=user, milestones=(0.25, 0.5, 0.75))
        for user in users[:3]:
            self._finish(story, user)
        # Audio: opened by 6, listened 30s+ by 4, halfway by 2.
        for index in range(6):
            visitor = f"a{index}"
            self._start(story, visitor_id=visitor, fmt="audio", milestones=(0.5,) if index < 2 else ())
            if index < 4:
                self._event(
                    AnalyticsEvent.EVENT_LISTENING_SESSION, story=story, visitor_id=visitor,
                    duration_seconds=90, metadata={"format": "audio"},
                )

        funnels = self._get(days=7)["funnels"]
        for fmt, steps in funnels.items():
            counts = [step["count"] for step in steps]
            self.assertEqual(counts, sorted(counts, reverse=True), fmt)
        read = {step["key"]: step for step in funnels["read"]}
        self.assertEqual([read[key]["count"] for key in ("viewers", "started", "half", "completed")], [20, 14, 10, 3])
        # Completion is signed-in only: 3 of the 4 signed-in readers who reached 50%.
        self.assertEqual(read["completed"]["population"], "signed_in")
        self.assertEqual((read["completed"]["base"], read["completed"]["rate"]), (4, 0.75))
        self.assertEqual(read["started"]["rate"], round(14 / 20, 4))
        listen = {step["key"]: step for step in funnels["listen"]}
        self.assertEqual([listen[key]["count"] for key in ("opened", "listened", "half", "completed")], [6, 4, 2, 0])

    def test_watch_funnel_is_flagged_partial_after_the_signed_in_only_steps(self):
        story = self._story("Watched")
        video = Video.objects.create(
            story=story, title="Narration", slug="narration", order=1,
            youtube_url="https://youtu.be/abcdefghijk", youtube_id="abcdefghijk",
        )
        users = [self._user() for _ in range(3)]
        for index in range(5):
            self._event(
                AnalyticsEvent.EVENT_WATCHING_SESSION, story=story, video=video, duration_seconds=120,
                visitor_id=f"w{index}", metadata={"format": "video"},
            )
        for user in users:
            self._event(
                AnalyticsEvent.EVENT_WATCHING_SESSION, story=story, video=video, duration_seconds=120,
                user=user, visitor_id=f"u{user.pk}", metadata={"format": "video"},
            )
        VideoWatchProgress.objects.create(user=users[0], story=story, video=video, progress=0.6)
        VideoWatchProgress.objects.create(user=users[1], story=story, video=video, progress=1.0)

        steps = {step["key"]: step for step in self._get(days=7)["funnels"]["watch"]}
        self.assertEqual([steps[key]["count"] for key in ("opened", "watched", "half", "completed")], [8, 8, 2, 1])
        self.assertFalse(steps["watched"]["partial"])
        self.assertTrue(steps["half"]["partial"])
        self.assertEqual((steps["half"]["base"], steps["half"]["rate"]), (3, round(2 / 3, 4)))

    # --- depth curve --------------------------------------------------------------

    def test_depth_curve_buckets_stories_by_length_not_by_chapter_position(self):
        short = self._story("Short One Chapter")
        Chapter.objects.create(story=short, title="Only", slug="only", content="x", order=1)
        Story.objects.filter(pk=short.pk).update(cached_chapter_reading_minutes=10)
        long = self._story("Long Many Chapters")
        for order in range(1, 9):
            Chapter.objects.create(story=long, title=f"C{order}", slug=f"c{order}", content="x", order=order)
        Story.objects.filter(pk=long.pk).update(cached_chapter_reading_minutes=90)
        medium = self._story("Medium By Chapters")
        for order in range(1, 4):
            Chapter.objects.create(story=medium, title=f"M{order}", slug=f"m{order}", content="x", order=order)
        # Chapter signals fill the estimate in; clear it to exercise the chapter-count fallback.
        Story.objects.filter(pk=medium.pk).update(cached_chapter_reading_minutes=None)
        unknown = self._story("Unknown Length")

        for milestones in ((0.25,), (0.25, 0.5), (), (0.25, 0.5, 0.75)):
            self._start(short, milestones=milestones)
        finisher = self._user()
        self._start(short, user=finisher, milestones=(0.25, 0.5, 0.75))
        self._finish(short, finisher)
        for _ in range(3):
            self._start(long, milestones=(0.25,))
        self._start(medium)
        self._start(unknown, milestones=(0.5,))

        curve = self._get(days=7)["depth_curve"]
        self.assertEqual((curve["short"]["starters"], curve["long"]["starters"], curve["medium"]["starters"]), (5, 3, 1))
        points = {point["pct"]: point for point in curve["short"]["points"]}
        self.assertEqual([points[pct]["rate"] for pct in (0, 25, 50, 75)], [1.0, 0.8, 0.6, 0.4])
        # 100% is signed-in only: the one signed-in starter finished.
        self.assertEqual((points[100]["rate"], points[100]["of"], points[100]["population"]), (1.0, 1, "signed_in"))
        self.assertEqual(curve["long"]["points"][1]["rate"], 1.0)
        self.assertEqual(curve["long"]["points"][2]["rate"], 0.0)
        # A story with no length estimate or chapters isn't in any bucket.
        self.assertEqual(sum(entry["starters"] for entry in curve.values()), 9)

    # --- drop-off stories -----------------------------------------------------------

    def test_drop_off_stories_need_five_starters_and_rank_lowest_finish_first(self):
        leaky = self._story("Leaky Tale")
        sticky = self._story("Sticky Tale")
        thin = self._story("Thin Tale")
        chapters = [
            Chapter.objects.create(story=leaky, title=f"Part {order}", slug=f"part-{order}", content="x", order=order)
            for order in range(1, 4)
        ]
        # 4 of 6 readers stop before 25%, 2 reach the last quarter.
        signed_in = [self._user() for _ in range(2)]
        for user in signed_in:
            self._start(leaky, user=user)
            ChapterReadingProgress.objects.create(user=user, story=leaky, chapter=chapters[1], progress=0.3)
        for index in range(2):
            self._start(leaky, visitor_id=f"l{index}")
        for index in range(2):
            self._start(leaky, visitor_id=f"far{index}", milestones=(0.25, 0.5, 0.75))
        for index in range(5):
            self._start(sticky, visitor_id=f"s{index}", milestones=(0.25, 0.5, 0.75))
        for index in range(4):
            self._start(thin, visitor_id=f"t{index}")

        rows = self._get(days=7)["drop_off_stories"]
        self.assertEqual([row["title"] for row in rows], ["Leaky Tale", "Sticky Tale"])
        leaky_row = rows[0]
        self.assertEqual((leaky_row["starters"], leaky_row["finish_rate"]), (6, round(2 / 6, 4)))
        self.assertEqual(leaky_row["stop_at"], "0-25%")
        self.assertEqual(leaky_row["stop_share"], round(4 / 6, 4))
        self.assertEqual(leaky_row["stop_chapter"], {"order": 2, "title": "Part 2"})
        self.assertEqual((leaky_row["completion_rate"], leaky_row["signed_in_starters"]), (0.0, 2))
        self.assertEqual(rows[1]["finish_rate"], 1.0)

    # --- previous period -------------------------------------------------------------

    def test_kpis_carry_previous_period_values_and_hide_small_previous_samples(self):
        story = self._story("Comparable")
        for index in range(5):
            self._start(story, visitor_id=f"now-{index}")
        self._view(story, 5)
        for index in range(5):
            self._start(story, visitor_id=f"then-{index}", days_ago=10)
        self._view(story, 10, days_ago=10)

        rate = self._get(days=7)["kpis"]["start_rate"]
        self.assertEqual((rate["value"], rate["sample_size"]), (1.0, 5))
        # Last week: 5 starters over 10 viewers; not "the last 14 days".
        self.assertEqual((rate["previous_value"], rate["previous_sample_size"]), (0.5, 10))

        cache.clear()
        AnalyticsEvent.objects.filter(visitor_id__in=["then-3", "then-4"]).delete()
        StoryView.objects.filter(ip_address__in=[f"198.51.100.{n}" for n in range(4, 11)]).delete()
        small = self._get(days=7)["kpis"]["start_rate"]
        self.assertIsNone(small["previous_value"])
        self.assertEqual(small["previous_sample_size"], 3)

        self.assertIsNone(self._get(days="all")["kpis"]["start_rate"]["previous_value"])

    def test_funnels_and_depth_curve_carry_previous_values(self):
        story = self._story("Both Periods")
        Story.objects.filter(pk=story.pk).update(cached_chapter_reading_minutes=10)
        for index in range(4):
            self._start(story, visitor_id=f"n{index}", milestones=(0.25,))
        for index in range(2):
            self._start(story, visitor_id=f"p{index}", days_ago=10)

        data = self._get(days=7)
        started = next(step for step in data["funnels"]["read"] if step["key"] == "started")
        self.assertEqual((started["count"], started["previous_count"]), (4, 2))
        self.assertEqual(data["depth_curve"]["short"]["points"][1]["rate"], 1.0)
        self.assertEqual(data["depth_curve"]["short"]["points"][1]["previous_rate"], 0.0)

    # --- audio, returning readers, retention -------------------------------------------

    def test_audio_listen_through_is_the_average_furthest_milestone_reached(self):
        story = self._story("Heard")
        for depth in ((), (0.25,), (0.5,), (0.75,), (0.25, 0.5, 0.75)):
            self._start(story, fmt="audio", milestones=depth)
        kpi = self._get(days=7)["kpis"]["audio_listen_through"]
        self.assertEqual(kpi["sample_size"], 5)
        self.assertEqual(kpi["value"], round((0 + 0.25 + 0.5 + 0.75 + 0.75) / 5, 3))

    def test_returning_readers_are_active_on_another_day_or_the_period_before(self):
        story = self._story("Come Back")
        for index in range(3):  # active twice in this window
            visitor = f"twice-{index}"
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, visitor_id=visitor, duration_seconds=60)
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, visitor_id=visitor, duration_seconds=60, days_ago=3)
        for index in range(2):  # seen last period too
            visitor = f"back-{index}"
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, visitor_id=visitor, duration_seconds=60)
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, visitor_id=visitor, duration_seconds=60, days_ago=10)
        for index in range(5):  # one visit only
            self._event(AnalyticsEvent.EVENT_READING_SESSION, story=story, visitor_id=f"once-{index}", duration_seconds=60)

        kpi = self._get(days=7)["kpis"]["returning_rate"]
        self.assertEqual((kpi["sample_size"], kpi["value"]), (10, 0.5))

    def test_retention_keeps_its_old_shape(self):
        retention = self._get(days=30)["retention"]
        self.assertEqual(set(retention), {"d1", "d7", "d30"})
        for bucket in retention.values():
            self.assertTrue({"eligible", "returned", "rate", "previous_rate"} <= set(bucket))

    # --- feedback ----------------------------------------------------------------------

    def test_feedback_signals_with_previous_totals_and_sparklines(self):
        story = self._story("Loved")
        users = [self._user() for _ in range(6)]
        for user in users[:3]:
            Favorite.objects.create(story=story, user=user)
        old = Favorite.objects.create(story=story, user=users[3])
        Favorite.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        for user, rating in zip(users[:5], (5, 5, 4, 3, 2)):
            Review.objects.create(story=story, user=user, rating=rating, comment="ok")
        for user in users[:2]:
            StoryReaction.objects.create(story=story, user=user, reaction_type="loved")
        Favorite.objects.create(story=story, user=self.staff)  # operators don't count

        feedback = self._get(days=7)["feedback"]
        favorites = feedback["favorites"]
        self.assertEqual((favorites["total"], favorites["previous_total"]), (3, 1))
        self.assertEqual(len(favorites["sparkline"]), 8)
        self.assertEqual(sum(favorites["sparkline"]), 3)
        reviews = feedback["reviews"]
        self.assertEqual((reviews["total"], reviews["avg_rating"]), (5, 3.8))
        self.assertEqual({row["rating"]: row["count"] for row in reviews["distribution"]}, {1: 0, 2: 1, 3: 1, 4: 1, 5: 2})
        self.assertEqual(feedback["reactions"]["total"], 2)

    def test_other_signals_keep_the_old_metrics_tabs_rates(self):
        story = self._story("Signals")
        self._event(AnalyticsEvent.EVENT_QUICK_READ_COMPLETED, story=story)
        self._event(AnalyticsEvent.EVENT_QUICK_READ_FULL_STORY_CLICKED, story=story)
        signals = self._get(days=7)["other_signals"]
        quick = next(row for row in signals["rates"] if row["key"] == "quick_read_conversion")
        self.assertEqual((quick["numerator"], quick["denominator"], quick["rate"]), (1, 1, 1.0))
        self.assertIn("surprise_me_clicked", signals["discovery"])

    def test_engagement_export_still_works(self):
        story = self._story("Exported Engagement")
        self._start(story)
        for file_format in ("csv", "xlsx"):
            response = self.client.get(
                reverse("admin-analytics-export"), {"sections": "engagement", "file_format": file_format, "days": 7}
            )
            self.assertEqual(response.status_code, 200, file_format)

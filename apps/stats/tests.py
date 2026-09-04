from datetime import date, timedelta
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.story.models import Audio, Video, Blog, Story, StoryView, Submission
from apps.users.models import User

from .models import AnalyticsEvent, VideoWatchProgress
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

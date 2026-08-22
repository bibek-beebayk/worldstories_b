from datetime import timedelta
from uuid import uuid4

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.story.models import Blog, Story
from apps.users.models import User

from .models import AnalyticsEvent


class AnalyticsEventApiTests(APITestCase):
    def setUp(self):
        cache.clear()
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
        Blog.objects.create(title="Published One", slug="published-one", content="<p>x</p>")
        Blog.objects.create(title="Published Two", slug="published-two", content="<p>x</p>")
        Blog.objects.create(
            title="Draft", slug="draft-post", content="<p>x</p>", is_published=False
        )

    def test_superuser_receives_blog_content_analytics(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-content"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["blog_posts_count"], 2)
        self.assertGreaterEqual(sum(row["count"] for row in response.data["blog_publishing_over_time"]), 2)

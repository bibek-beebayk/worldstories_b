from datetime import timedelta
from uuid import uuid4

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.story.models import Story
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

        first_visit = AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="returning-browser",
            session_id="session-one",
        )
        AnalyticsEvent.objects.filter(pk=first_visit.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_VISIT,
            visitor_id="returning-browser",
            session_id="session-two",
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

    def test_superuser_receives_aggregated_audience_analytics(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("admin-analytics-audience"), {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["visitors"], 1)
        self.assertEqual(response.data["summary"]["returning_visitors"], 1)
        self.assertEqual(response.data["summary"]["ad_impressions"], 1)
        self.assertEqual(response.data["summary"]["downloads"], 1)
        self.assertEqual(response.data["summary"]["completions"], 1)
        self.assertEqual(response.data["summary"]["readers"], 1)
        self.assertEqual(response.data["summary"]["returning_readers"], 1)
        self.assertEqual(response.data["summary"]["reader_retention_rate"], 1)
        self.assertEqual(response.data["summary"]["completion_rate"], 1)
        self.assertEqual(response.data["summary"]["reading_minutes"], 3)
        self.assertEqual(response.data["summary"]["listening_minutes"], 3)
        self.assertEqual(response.data["top_downloads"][0]["slug"], self.story.slug)

    def test_non_superuser_is_forbidden(self):
        self.client.force_authenticate(self.reader)

        response = self.client.get(reverse("admin-analytics-audience"))

        self.assertEqual(response.status_code, 403)

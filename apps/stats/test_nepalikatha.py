from datetime import timedelta
from uuid import uuid4
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from apps.stats.models import NepalikathaEvent, AnalyticsEvent, ReadingProgress, StoryCompletion
from apps.story.models import Story, Chapter, StoryView, StoryType
from apps.story.analytics_api import build_audience_data, build_engagement_data
from apps.users.models import User

BROWSER = "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36"
ORIGIN = "https://nepalikatha.worldstories.net"


class NepalikathaAnalyticsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(HTTP_USER_AGENT=BROWSER, HTTP_ORIGIN=ORIGIN)
        self.story = Story.objects.create(story_type=StoryType.objects.create(name="Nepali test type"), title="Nepali story", slug="np-story", is_published=True, show_in_nepali_site=True)
        self.chapter = Chapter.objects.create(story=self.story, title="One", slug="one", order=1, content="<p>Text</p>")
        self.admin = User.objects.create_user(email="np-admin@example.com", username="np-admin", password="password", is_superuser=True, is_staff=True)
        self.visitor = str(uuid4())
        self.session = str(uuid4())

    def payload(self, kind="visit", **extra):
        values = {"event_id": str(uuid4()), "event_type": kind, "visitor_id": self.visitor,
                  "session_id": self.session, "path": "/katha/np-story"}
        if kind == "read":
            values.update(story_slug=self.story.slug, chapter_slug=self.chapter.slug, duration_seconds=15)
        return {**values, **extra}

    def send(self, payload):
        return self.client.post("/api/nepalikatha/events/", payload, format="json")

    def report(self, days=30):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(f"/api/admin/analytics/nepalikatha/?days={days}")
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_ingestion_is_idempotent_and_does_not_change_main_analytics(self):
        before = (build_audience_data(30), build_engagement_data(30))
        payload = self.payload("read")
        self.assertEqual(self.send(payload).status_code, 202)
        self.assertEqual(self.send(payload).status_code, 202)
        self.assertEqual(NepalikathaEvent.objects.count(), 1)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)
        self.assertEqual(StoryView.objects.count(), 0)
        self.assertEqual(ReadingProgress.objects.count(), 0)
        self.assertEqual(StoryCompletion.objects.count(), 0)
        self.story.refresh_from_db()
        self.assertEqual(self.story.views, 0)
        self.assertEqual(before, (build_audience_data(30), build_engagement_data(30)))

    def test_legacy_main_site_endpoints_ignore_nepali_origin_but_still_track_main_site(self):
        payload = {"event_type": "visit", "event_id": str(uuid4()), "visitor_id": "legacy", "session_id": "session", "metadata": {"path": "/"}}
        self.client.post("/api/analytics/events/", payload, format="json")
        self.client.post(f"/api/stories/{self.story.slug}/view/", {}, format="json")
        self.assertFalse(AnalyticsEvent.objects.exists())
        self.assertFalse(StoryView.objects.exists())
        main = APIClient(HTTP_USER_AGENT=BROWSER, HTTP_ORIGIN="https://worldstories.net")
        self.assertEqual(main.post("/api/analytics/events/", payload, format="json").status_code, 201)
        main.post(f"/api/stories/{self.story.slug}/view/", {}, format="json")
        self.assertEqual(AnalyticsEvent.objects.count(), 1)
        self.assertEqual(StoryView.objects.count(), 1)
        self.assertFalse(NepalikathaEvent.objects.exists())

    def test_visitors_reads_and_time_are_aggregated_without_counting_heartbeats_as_reads(self):
        self.send(self.payload())
        self.send(self.payload("read"))
        chapter2 = Chapter.objects.create(story=self.story, title="Two", slug="two", order=2, content="Text")
        self.send(self.payload("read", chapter_slug=chapter2.slug))
        visitor2, session2 = str(uuid4()), self.session
        self.send(self.payload(visitor_id=visitor2, session_id=session2))
        self.send(self.payload("read", visitor_id=visitor2, session_id=session2, duration_seconds=30))
        report = self.report()
        self.assertEqual((report["visitors"], report["page_views"], report["readers"]), (2, 2, 2))
        self.assertEqual((report["stories_read"], report["reading_sessions"], report["reading_seconds"]), (1, 2, 60))
        self.assertEqual(report["average_reading_seconds"], 30)
        self.assertEqual(report["top_stories"][0]["reads"], 2)
        self.assertEqual(sum(row["reading_seconds"] for row in report["over_time"]), 60)

    def test_unselected_drafts_wrong_chapters_and_bad_durations_are_rejected(self):
        for values in ({"duration_seconds": 31}, {"duration_seconds": 0}, {"chapter_slug": "missing"}, {"story_slug": "missing"}, {"visitor_id": "invalid"}):
            self.assertEqual(self.send(self.payload("read", **values)).status_code, 400)
        self.story.show_in_nepali_site = False
        self.story.save()
        self.assertEqual(self.send(self.payload("read")).status_code, 400)
        self.story.show_in_nepali_site = True
        self.story.is_published = False
        self.story.save()
        self.assertEqual(self.send(self.payload("read")).status_code, 400)
        self.assertFalse(NepalikathaEvent.objects.exists())

    def test_bot_and_authenticated_operator_activity_is_ignored(self):
        bot = APIClient(HTTP_USER_AGENT="Googlebot", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(bot.post("/api/nepalikatha/events/", self.payload(), format="json").status_code, 202)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.send(self.payload()).status_code, 202)
        self.assertFalse(NepalikathaEvent.objects.exists())

    def test_report_requires_superuser_and_respects_range_and_empty_data(self):
        self.assertIn(self.client.get("/api/admin/analytics/nepalikatha/").status_code, (401, 403))
        staff = User.objects.create_user(email="np-staff@example.com", username="np-staff", password="password", is_staff=True)
        client = APIClient()
        client.force_authenticate(staff)
        self.assertEqual(client.get("/api/admin/analytics/nepalikatha/").status_code, 403)
        self.send(self.payload("read"))
        NepalikathaEvent.objects.update(created_at=timezone.now() - timedelta(days=8))
        report = self.report(7)
        self.assertEqual(report["reading_seconds"], 0)
        self.assertIsNone(report["average_reading_seconds"])
        self.assertEqual(report["top_stories"], [])
        self.assertEqual(self.report(30)["reading_seconds"], 15)
        self.assertEqual(self.report(999)["range_days"], 30)

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.story.models import Story
from apps.users.models import User

from .models import DigestDelivery, DigestIssue, NewsletterSubscription

ADMIN_TEST_SETTINGS = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    NEWSLETTER_FROM_EMAIL="WorldStories <digest@example.com>",
    NEWSLETTER_REPLY_TO="reply@example.com",
    NEWSLETTER_SITE_URL="https://worldstories.net",
    NEWSLETTER_API_URL="https://api.worldstories.net",
    NEWSLETTER_POSTAL_ADDRESS="WorldStories, Kathmandu, Nepal",
    NEWSLETTER_SEND_DELAY_SECONDS=0,
    NEWSLETTER_DAILY_SEND_LIMIT=80,
    NEWSLETTER_SEND_TIMEZONE="UTC",
)


@override_settings(**ADMIN_TEST_SETTINGS)
class NewsletterAdminApiTests(APITestCase):
    def setUp(self):
        self.superuser = User.objects.create_user(
            email="admin@example.com",
            username="newsletteradmin",
            password="password",
            is_superuser=True,
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            email="regular@example.com",
            username="regularuser",
            password="password",
            is_superuser=False,
            is_staff=True,
        )
        self.story = Story.objects.create(
            title="Admin Digest Story",
            slug="admin-digest-story",
            is_published=True,
            site_published_date=timezone.localdate(),
        )
        now = timezone.now()
        self.confirmed = NewsletterSubscription.objects.create(
            email="confirmed@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            source="blog",
            send_weekday=5,
            confirm_token=None,
            confirmed_at=now - timedelta(days=2),
        )
        self.pending = NewsletterSubscription.objects.create(
            email="pending@example.com",
            status=NewsletterSubscription.STATUS_PENDING,
            source="footer",
            send_weekday=None,
        )
        self.unsubscribed = NewsletterSubscription.objects.create(
            email="unsub@example.com",
            status=NewsletterSubscription.STATUS_UNSUBSCRIBED,
            source="profile",
            send_weekday=1,
            confirm_token=None,
            unsubscribed_at=now - timedelta(days=1),
        )
        self.issue = DigestIssue.objects.create(
            issue_date=timezone.localdate(),
            subject="New this week: Admin Digest Story",
            featured_story=self.story,
            status=DigestIssue.STATUS_FAILED,
            sent_count=0,
            failed_count=1,
            started_at=now - timedelta(hours=1),
        )
        self.failed_delivery = DigestDelivery.objects.create(
            issue=self.issue,
            subscription=self.confirmed,
            status=DigestDelivery.STATUS_FAILED,
            error="Temporary provider outage",
        )

    def _assert_no_tokens_in_text(self, text: str):
        for sub in NewsletterSubscription.objects.all():
            if sub.confirm_token:
                self.assertNotIn(sub.confirm_token, text)
            if sub.unsubscribe_token:
                self.assertNotIn(sub.unsubscribe_token, text)
            if sub.confirmed_token_hash:
                self.assertNotIn(sub.confirmed_token_hash, text)
        self.assertNotIn("confirm_token", text)
        self.assertNotIn("unsubscribe_token", text)
        self.assertNotIn("confirmed_token_hash", text)

    def test_permissions_across_all_endpoints(self):
        get_urls = [
            "/api/admin/newsletter/stats/",
            "/api/admin/newsletter/subscribers/",
            "/api/admin/newsletter/subscribers/export/",
            "/api/admin/newsletter/issues/",
            f"/api/admin/newsletter/issues/{self.issue.pk}/deliveries/",
            "/api/admin/newsletter/preview/",
        ]
        post_urls = [
            f"/api/admin/newsletter/subscribers/{self.pending.pk}/resend-confirmation/",
            f"/api/admin/newsletter/subscribers/{self.confirmed.pk}/unsubscribe/",
            f"/api/admin/newsletter/issues/{self.issue.pk}/retry-failed/",
            "/api/admin/newsletter/test-send/",
        ]

        # Unauthenticated -> 401/403
        for url in get_urls:
            resp = self.client.get(url)
            self.assertIn(resp.status_code, (401, 403), msg=url)
        for url in post_urls:
            resp = self.client.post(url, {}, format="json")
            self.assertIn(resp.status_code, (401, 403), msg=url)

        # Non-superuser staff -> 403
        self.client.force_authenticate(user=self.regular_user)
        for url in get_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 403, msg=url)
        for url in post_urls:
            resp = self.client.post(url, {}, format="json")
            self.assertEqual(resp.status_code, 403, msg=url)

        # Superuser -> 200 on every endpoint, and tokens never appear
        self.client.force_authenticate(user=self.superuser)
        for url in get_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, msg=url)
            body = (
                b"".join(resp.streaming_content).decode("utf-8")
                if getattr(resp, "streaming", False)
                else resp.content.decode("utf-8")
            )
            self._assert_no_tokens_in_text(body)

        for url in post_urls:
            resp = self.client.post(url, {}, format="json")
            self.assertEqual(resp.status_code, 200, msg=url)
            self._assert_no_tokens_in_text(resp.content.decode("utf-8"))

    def test_subscribers_filters_and_pagination(self):
        self.client.force_authenticate(user=self.superuser)
        resp_q = self.client.get("/api/admin/newsletter/subscribers/?q=CONFIRMED")
        self.assertEqual(resp_q.status_code, 200)
        self.assertEqual(resp_q.data["count"], 1)
        self.assertEqual(resp_q.data["results"][0]["email"], "confirmed@example.com")

        resp_status = self.client.get("/api/admin/newsletter/subscribers/?status=pending")
        self.assertEqual(resp_status.data["count"], 1)
        self.assertEqual(resp_status.data["results"][0]["email"], "pending@example.com")

        resp_source = self.client.get("/api/admin/newsletter/subscribers/?source=profile")
        self.assertEqual(resp_source.data["count"], 1)
        self.assertEqual(resp_source.data["results"][0]["email"], "unsub@example.com")

        resp_day = self.client.get("/api/admin/newsletter/subscribers/?weekday=5")
        self.assertEqual(resp_day.data["count"], 1)
        self.assertEqual(resp_day.data["results"][0]["email"], "confirmed@example.com")

        resp_page = self.client.get("/api/admin/newsletter/subscribers/?page=1&page_size=2")
        self.assertEqual(resp_page.data["count"], 3)
        self.assertEqual(resp_page.data["page"], 1)
        self.assertEqual(resp_page.data["page_size"], 2)
        self.assertEqual(len(resp_page.data["results"]), 2)

        resp_page2 = self.client.get("/api/admin/newsletter/subscribers/?page=2&page_size=2")
        self.assertEqual(len(resp_page2.data["results"]), 1)

    def test_export_csv_headers_and_formula_escape(self):
        self.client.force_authenticate(user=self.superuser)
        NewsletterSubscription.objects.create(
            email="=cmd@example.com",
            status=NewsletterSubscription.STATUS_PENDING,
            source="other",
        )
        resp = self.client.get("/api/admin/newsletter/subscribers/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        self.assertIn("newsletter-subscribers-", resp["Content-Disposition"])
        content = b"".join(resp.streaming_content).decode("utf-8")
        lines = [line for line in content.splitlines() if line]
        self.assertEqual(
            lines[0],
            "email,status,source,language,send_weekday,created_at,confirmed_at",
        )
        self.assertTrue(any(line.startswith("'=cmd@example.com,") for line in lines[1:]))
        self._assert_no_tokens_in_text(content)

    def test_resend_confirmation_pending_only(self):
        self.client.force_authenticate(user=self.superuser)
        ok_resp = self.client.post(
            f"/api/admin/newsletter/subscribers/{self.pending.pk}/resend-confirmation/"
        )
        self.assertEqual(ok_resp.status_code, 200)
        self.assertEqual(ok_resp.data, {"detail": "Confirmation email sent."})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.pending.email])

        bad_resp = self.client.post(
            f"/api/admin/newsletter/subscribers/{self.confirmed.pk}/resend-confirmation/"
        )
        self.assertEqual(bad_resp.status_code, 400)
        self.assertIn("detail", bad_resp.data)

    def test_unsubscribe_endpoint_updates_status(self):
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post(
            f"/api/admin/newsletter/subscribers/{self.confirmed.pk}/unsubscribe/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "unsubscribed")
        self.confirmed.refresh_from_db()
        self.assertEqual(self.confirmed.status, NewsletterSubscription.STATUS_UNSUBSCRIBED)
        self.assertIsNotNone(self.confirmed.unsubscribed_at)

    def test_stats_counts_match_fixtures(self):
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.get("/api/admin/newsletter/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["confirmed"], 1)
        self.assertEqual(resp.data["pending"], 1)
        self.assertEqual(resp.data["unsubscribed"], 1)
        self.assertEqual(resp.data["total"], 3)
        self.assertEqual(resp.data["new_this_week"], 1)
        self.assertEqual(resp.data["unsubscribe_rate"], round(1 / 3, 3))
        self.assertEqual(len(resp.data["weekly_growth"]), 12)
        self.assertEqual(sum(w["confirmed"] for w in resp.data["weekly_growth"]), 1)
        self.assertEqual(len(resp.data["weekday_load"]), 7)
        sat = next(d for d in resp.data["weekday_load"] if d["weekday"] == 5)
        self.assertEqual(sat, {"weekday": 5, "label": "Sat", "count": 1})
        self.assertEqual(resp.data["daily_send_limit"], 80)

    @override_settings(NEWSLETTER_DAILY_SEND_LIMIT=2)
    def test_retry_failed_respects_daily_limit(self):
        self.client.force_authenticate(user=self.superuser)
        # Add two more confirmed subscribers with failed deliveries on the same issue (3 failed total)
        extra_subs = [
            NewsletterSubscription.objects.create(
                email=f"retry{idx}@example.com",
                status=NewsletterSubscription.STATUS_CONFIRMED,
                send_weekday=5,
                confirm_token=None,
            )
            for idx in range(2)
        ]
        for sub in extra_subs:
            DigestDelivery.objects.create(
                issue=self.issue,
                subscription=sub,
                status=DigestDelivery.STATUS_FAILED,
                error="Timeout",
            )
        self.issue.failed_count = 3
        self.issue.save(update_fields=["failed_count"])

        resp = self.client.post(f"/api/admin/newsletter/issues/{self.issue.pk}/retry-failed/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.data,
            {"attempted": 2, "sent": 2, "failed": 0, "remaining_failed": 1},
        )
        self.assertEqual(len(mail.outbox), 2)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.sent_count, 2)
        self.assertEqual(self.issue.failed_count, 1)

        # Second call on the same day sees daily limit already reached -> 0 attempted
        resp2 = self.client.post(f"/api/admin/newsletter/issues/{self.issue.pk}/retry-failed/")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(
            resp2.data,
            {"attempted": 0, "sent": 0, "failed": 0, "remaining_failed": 1},
        )

    def test_test_send_uses_outbox_and_goes_to_superuser_email(self):
        self.client.force_authenticate(user=self.superuser)
        resp = self.client.post("/api/admin/newsletter/test-send/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"detail": f"Test digest sent to {self.superuser.email}."})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.superuser.email])

    def test_preview_returns_html_or_empty_reason_without_side_effects(self):
        self.client.force_authenticate(user=self.superuser)
        before_issues = DigestIssue.objects.count()
        before_deliveries = DigestDelivery.objects.count()
        resp = self.client.get("/api/admin/newsletter/preview/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["subject"])
        self.assertIn("<html", resp.data["html"].lower())
        self.assertEqual(DigestIssue.objects.count(), before_issues)
        self.assertEqual(DigestDelivery.objects.count(), before_deliveries)

        Story.objects.update(site_published_date=timezone.localdate() - timedelta(days=30))
        empty_resp = self.client.get("/api/admin/newsletter/preview/")
        self.assertEqual(empty_resp.status_code, 200)
        self.assertEqual(
            empty_resp.data,
            {"subject": None, "html": None, "reason": "No new stories this week"},
        )

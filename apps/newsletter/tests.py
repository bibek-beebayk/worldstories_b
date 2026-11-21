from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.story.models import Story
from apps.users.models import User

from .models import DigestDelivery, DigestIssue, NewsletterSubscription
from .services import assign_send_weekday, select_digest_audience


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    NEWSLETTER_FROM_EMAIL="WorldStories <digest@example.com>",
    NEWSLETTER_SITE_URL="https://worldstories.net",
)
class NewsletterApiTests(APITestCase):
    def setUp(self):
        cache.clear()

    def subscribe(self, email="reader@example.com", source="footer"):
        return self.client.post(
            reverse("newsletter:subscribe"),
            {"email": email, "source": source},
            format="json",
        )

    def test_subscribe_creates_pending_and_sends_one_confirmation(self):
        response = self.subscribe()
        subscription = NewsletterSubscription.objects.get(email="reader@example.com")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(subscription.status, NewsletterSubscription.STATUS_PENDING)
        self.assertEqual(subscription.confirmation_send_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(subscription.confirm_token, mail.outbox[0].body)

    def test_subscribe_response_is_generic_for_confirmed_address(self):
        NewsletterSubscription.objects.create(
            email="reader@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.data,
            {
                "detail": "If this address can be subscribed, we sent a confirmation link."
            },
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_confirmation_resend_is_capped_at_three_per_24_hours(self):
        for _ in range(5):
            self.subscribe()
        subscription = NewsletterSubscription.objects.get(email="reader@example.com")
        self.assertEqual(subscription.confirmation_send_count, 3)
        self.assertEqual(len(mail.outbox), 3)

    def test_confirm_happy_path_and_reused_token(self):
        self.subscribe()
        subscription = NewsletterSubscription.objects.get(email="reader@example.com")
        token = subscription.confirm_token
        url = reverse("newsletter:confirm")
        first = self.client.post(url, {"token": token}, format="json")
        second = self.client.post(url, {"token": token}, format="json")
        subscription.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(subscription.status, NewsletterSubscription.STATUS_CONFIRMED)
        self.assertIsNone(subscription.confirm_token)

    def test_expired_confirmation_is_rejected(self):
        self.subscribe()
        subscription = NewsletterSubscription.objects.get(email="reader@example.com")
        NewsletterSubscription.objects.filter(pk=subscription.pk).update(
            confirmation_sent_at=timezone.now() - timedelta(days=8)
        )
        response = self.client.post(
            reverse("newsletter:confirm"),
            {"token": subscription.confirm_token},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_or_expired")

    def test_subscribing_after_unsubscribe_requires_fresh_confirmation(self):
        subscription = NewsletterSubscription.objects.create(
            email="reader@example.com",
            status=NewsletterSubscription.STATUS_UNSUBSCRIBED,
        )
        old_token = subscription.confirm_token
        response = self.subscribe()
        subscription.refresh_from_db()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(subscription.status, NewsletterSubscription.STATUS_PENDING)
        self.assertNotEqual(subscription.confirm_token, old_token)
        self.assertEqual(len(mail.outbox), 1)

    def test_logged_in_toggle_links_pending_row_and_turns_off(self):
        user = User.objects.create_user(
            email="reader@example.com", username="reader", password="password"
        )
        subscription = NewsletterSubscription.objects.create(
            email=user.email, status=NewsletterSubscription.STATUS_PENDING
        )
        self.client.force_authenticate(user)
        enabled = self.client.post(
            reverse("newsletter:me"),
            {"subscribed": True, "source": "profile"},
            format="json",
        )
        subscription.refresh_from_db()
        self.assertEqual(enabled.data["status"], "confirmed")
        self.assertEqual(subscription.user, user)
        disabled = self.client.post(
            reverse("newsletter:me"), {"subscribed": False}, format="json"
        )
        subscription.refresh_from_db()
        self.assertEqual(disabled.data["status"], "unsubscribed")
        self.assertIsNotNone(subscription.unsubscribed_at)

    def test_logged_in_toggle_accepts_every_model_source_choice(self):
        user = User.objects.create_user(
            email="reader@example.com", username="reader", password="password"
        )
        self.client.force_authenticate(user)

        for source, _label in NewsletterSubscription.SOURCE_CHOICES:
            with self.subTest(source=source):
                response = self.client.post(
                    reverse("newsletter:me"),
                    {"subscribed": True, "source": source},
                    format="json",
                )
                self.assertEqual(response.status_code, 200)
                subscription = NewsletterSubscription.objects.get(email=user.email)
                self.assertEqual(subscription.source, source)

    def test_one_click_unsubscribe_accepts_form_body_and_is_idempotent(self):
        subscription = NewsletterSubscription.objects.create(
            email="reader@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        url = reverse("newsletter:unsubscribe", args=[subscription.unsubscribe_token])
        first = self.client.post(
            url,
            "List-Unsubscribe=One-Click",
            content_type="application/x-www-form-urlencoded",
        )
        second = self.client.post(url, {"reason": "Too frequent"}, format="json")
        subscription.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(subscription.unsubscribe_reason, "Too frequent")

    def test_unsubscribe_by_email_is_generic_for_known_and_unknown(self):
        subscription = NewsletterSubscription.objects.create(
            email="reader@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        url = reverse("newsletter:unsubscribe-by-email")
        known = self.client.post(url, {"email": subscription.email}, format="json")
        unknown = self.client.post(url, {"email": "unknown@example.com"}, format="json")
        self.assertEqual(known.data, unknown.data)
        subscription.refresh_from_db()
        self.assertEqual(
            subscription.status, NewsletterSubscription.STATUS_UNSUBSCRIBED
        )

    def test_token_endpoints_return_404_for_garbage(self):
        unsubscribe = self.client.post(
            reverse("newsletter:unsubscribe", args=["garbage"]), {}, format="json"
        )
        resubscribe = self.client.post(
            reverse("newsletter:resubscribe", args=["garbage"]), {}, format="json"
        )
        self.assertEqual(unsubscribe.status_code, 404)
        self.assertEqual(resubscribe.status_code, 404)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    NEWSLETTER_FROM_EMAIL="WorldStories <digest@example.com>",
    NEWSLETTER_SITE_URL="https://worldstories.net",
)
class NewsletterWeekdayAndApiTests(APITestCase):
    def setUp(self):
        cache.clear()

    # E1: assign_send_weekday
    def test_assign_send_weekday_empty_balanced_and_tie_break(self):
        self.assertEqual(assign_send_weekday(), 5)

        expected_order = [5, 6, 4, 3, 2, 1, 0]
        assigned = []
        for idx in range(7):
            day = assign_send_weekday()
            assigned.append(day)
            NewsletterSubscription.objects.create(
                email=f"sub{idx}@example.com",
                status=NewsletterSubscription.STATUS_CONFIRMED,
                confirm_token=None,
                send_weekday=day,
            )
        self.assertEqual(assigned, expected_order)

        # Once all 7 weekdays have 1 confirmed subscriber, tie-break restarts at Saturday (5).
        self.assertEqual(assign_send_weekday(), 5)

        # Unconfirmed subscriptions do not affect the count.
        NewsletterSubscription.objects.create(
            email="pending@example.com",
            status=NewsletterSubscription.STATUS_PENDING,
            send_weekday=5,
        )
        self.assertEqual(assign_send_weekday(), 5)

    # E2: weekday assignment on confirm, /me subscribe, and resubscribe
    def test_weekday_assigned_on_confirm_me_and_resubscribe_preserves_existing(self):
        # 1. Confirm assigns a weekday and returns it
        self.client.post(
            reverse("newsletter:subscribe"),
            {"email": "confirm-user@example.com", "source": "footer"},
            format="json",
        )
        sub = NewsletterSubscription.objects.get(email="confirm-user@example.com")
        self.assertIsNone(sub.send_weekday)
        confirm_resp = self.client.post(
            reverse("newsletter:confirm"), {"token": sub.confirm_token}, format="json"
        )
        self.assertEqual(confirm_resp.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.send_weekday, 5)
        self.assertEqual(confirm_resp.data["send_weekday"], 5)

        # 2. Logged-in /me subscribe assigns next weekday (6 = Sunday)
        user = User.objects.create_user(
            email="me-user@example.com", username="meuser", password="password"
        )
        self.client.force_authenticate(user)
        me_before = self.client.get(reverse("newsletter:me"))
        self.assertEqual(me_before.status_code, 200)
        self.assertIsNone(me_before.data["send_weekday"])

        me_sub = self.client.post(
            reverse("newsletter:me"),
            {"subscribed": True, "source": "profile"},
            format="json",
        )
        self.assertEqual(me_sub.status_code, 200)
        self.assertEqual(me_sub.data["send_weekday"], 6)
        me_after = self.client.get(reverse("newsletter:me"))
        self.assertEqual(me_after.data["send_weekday"], 6)

        # 3. Resubscribe assigns a weekday if missing
        self.client.force_authenticate(user=None)
        unsub_no_day = NewsletterSubscription.objects.create(
            email="resub-null@example.com",
            status=NewsletterSubscription.STATUS_UNSUBSCRIBED,
            confirm_token=None,
            send_weekday=None,
        )
        resub1 = self.client.post(
            reverse("newsletter:resubscribe", args=[unsub_no_day.unsubscribe_token]),
            {},
            format="json",
        )
        self.assertEqual(resub1.status_code, 200)
        unsub_no_day.refresh_from_db()
        self.assertEqual(unsub_no_day.send_weekday, 4)

        # 4. Resubscribe preserves existing send_weekday
        unsub_with_day = NewsletterSubscription.objects.create(
            email="resub-keep@example.com",
            status=NewsletterSubscription.STATUS_UNSUBSCRIBED,
            confirm_token=None,
            send_weekday=2,
        )
        resub2 = self.client.post(
            reverse("newsletter:resubscribe", args=[unsub_with_day.unsubscribe_token]),
            {},
            format="json",
        )
        self.assertEqual(resub2.status_code, 200)
        unsub_with_day.refresh_from_db()
        self.assertEqual(unsub_with_day.send_weekday, 2)

    # E3: send_weekday API validation
    def test_send_weekday_api_validation(self):
        user = User.objects.create_user(
            email="val@example.com", username="valuser", password="password"
        )
        self.client.force_authenticate(user)
        url = reverse("newsletter:me")
        self.client.post(url, {"subscribed": True}, format="json")

        for valid_day in range(7):
            resp = self.client.post(url, {"send_weekday": valid_day}, format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data["send_weekday"], valid_day)

        for invalid_value in (7, -1, "x"):
            with self.subTest(value=invalid_value):
                resp = self.client.post(
                    url, {"send_weekday": invalid_value}, format="json"
                )
                self.assertEqual(resp.status_code, 400)


DIGEST_TEST_SETTINGS = dict(
    EMAIL_BACKEND="apps.newsletter.tests_digest.RecordingEmailBackend",
    NEWSLETTER_FROM_EMAIL="WorldStories <digest@example.com>",
    NEWSLETTER_REPLY_TO="reply@example.com",
    NEWSLETTER_SITE_URL="https://worldstories.net",
    NEWSLETTER_API_URL="https://api.worldstories.net",
    NEWSLETTER_POSTAL_ADDRESS="WorldStories, Kathmandu, Nepal",
    NEWSLETTER_SEND_DELAY_SECONDS=0,
    NEWSLETTER_DAILY_SEND_LIMIT=80,
    NEWSLETTER_SEND_TIMEZONE="UTC",
)


@override_settings(**DIGEST_TEST_SETTINGS)
class NewsletterCohortDigestTests(TestCase):
    def setUp(self):
        # 2026-11-23 is a Monday (weekday=0)
        self.monday = datetime(2026, 11, 23, 9, 0, tzinfo=dt_timezone.utc)
        self.tuesday = datetime(2026, 11, 24, 9, 0, tzinfo=dt_timezone.utc)
        self.next_monday = datetime(2026, 11, 30, 9, 0, tzinfo=dt_timezone.utc)
        self.story = Story.objects.create(
            title="Cohort Story",
            slug="cohort-story",
            is_published=True,
            site_published_date=self.monday.date(),
            rating=5,
            views=100,
        )

    # E4: Audience selection
    def test_audience_selection_due_today_recent_and_overdue_ordering(self):
        due_today = NewsletterSubscription.objects.create(
            email="due@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=0,
            confirmed_at=self.monday - timedelta(days=2),
            last_digest_sent_at=None,
        )
        sent_recently = NewsletterSubscription.objects.create(
            email="recent@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=0,
            confirmed_at=self.monday - timedelta(days=20),
            last_digest_sent_at=self.monday - timedelta(days=3),
        )
        overdue_other_day = NewsletterSubscription.objects.create(
            email="overdue@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=3,
            confirmed_at=self.monday - timedelta(days=20),
            last_digest_sent_at=self.monday - timedelta(days=9),
        )
        not_due_other_day = NewsletterSubscription.objects.create(
            email="otherday@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=4,
            confirmed_at=self.monday - timedelta(days=2),
            last_digest_sent_at=None,
        )

        plan = select_digest_audience(now=self.monday, assign_missing=False)
        emails = [sub.email for sub in plan["audience"]]
        self.assertEqual(emails, [overdue_other_day.email, due_today.email])
        self.assertNotIn(sent_recently.email, emails)
        self.assertNotIn(not_due_other_day.email, emails)

    # E5: Daily cap across runs and carryover to next day
    @override_settings(NEWSLETTER_DAILY_SEND_LIMIT=2)
    def test_daily_cap_across_runs_and_next_day_carryover(self):
        subs = [
            NewsletterSubscription.objects.create(
                email=f"cap{i}@example.com",
                status=NewsletterSubscription.STATUS_CONFIRMED,
                confirm_token=None,
                send_weekday=0,
                confirmed_at=self.monday - timedelta(days=14),
                last_digest_sent_at=self.monday - timedelta(days=7),
            )
            for i in range(3)
        ]

        # First run on Monday sends to 2 of 3 (cap = 2)
        out1 = StringIO()
        with patch("django.utils.timezone.now", return_value=self.monday):
            call_command("send_weekly_digest", stdout=out1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("1 subscriber(s) remain", out1.getvalue())

        # Second run on Monday cannot exceed the daily cap
        out2 = StringIO()
        with patch(
            "django.utils.timezone.now",
            return_value=self.monday + timedelta(hours=2),
        ):
            call_command("send_weekly_digest", stdout=out2)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Daily cap reached", out2.getvalue())

        # Next day (Tuesday): the left-over person is now 8 days since last send -> overdue -> sent!
        out3 = StringIO()
        with patch("django.utils.timezone.now", return_value=self.tuesday):
            call_command("send_weekly_digest", stdout=out3)
        self.assertEqual(len(mail.outbox), 3)
        sent_recipients = {msg.to[0] for msg in mail.outbox}
        self.assertEqual(sent_recipients, {s.email for s in subs})

    # E6: Story of the week reused within ISO week and changes next ISO week
    def test_story_of_week_reused_same_iso_week_and_rotates_next_week(self):
        second_story = Story.objects.create(
            title="Second Story",
            slug="second-story",
            is_published=True,
            site_published_date=self.monday.date(),
            rating=4,
            views=50,
        )
        NewsletterSubscription.objects.create(
            email="mon@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=0,
            confirmed_at=self.monday - timedelta(days=2),
        )
        NewsletterSubscription.objects.create(
            email="tue@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=1,
            confirmed_at=self.monday - timedelta(days=2),
        )

        with patch("django.utils.timezone.now", return_value=self.monday):
            call_command("send_weekly_digest", stdout=StringIO())
        with patch("django.utils.timezone.now", return_value=self.tuesday):
            call_command("send_weekly_digest", stdout=StringIO())

        mon_issue = DigestIssue.objects.get(issue_date=self.monday.date())
        tue_issue = DigestIssue.objects.get(issue_date=self.tuesday.date())
        self.assertEqual(mon_issue.featured_story_id, self.story.id)
        self.assertEqual(tue_issue.featured_story_id, self.story.id)

        # Next week (new ISO week): create a fresh story so the window isn't empty
        Story.objects.create(
            title="Week Two Story",
            slug="week-two-story",
            is_published=True,
            site_published_date=self.next_monday.date(),
            rating=1,
            views=5,
        )
        with patch("django.utils.timezone.now", return_value=self.next_monday):
            call_command("send_weekly_digest", stdout=StringIO())
        next_mon_issue = DigestIssue.objects.get(issue_date=self.next_monday.date())
        self.assertEqual(next_mon_issue.featured_story_id, second_story.id)
        self.assertNotEqual(next_mon_issue.featured_story_id, mon_issue.featured_story_id)

    # E7: Skip-when-empty creates no rows; --dry-run sends nothing, writes nothing, prints counts
    @override_settings(NEWSLETTER_DAILY_SEND_LIMIT=1)
    def test_skip_when_empty_and_dry_run(self):
        unassigned = NewsletterSubscription.objects.create(
            email="unassigned@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=None,
            confirmed_at=self.monday - timedelta(days=10),
        )
        NewsletterSubscription.objects.create(
            email="due-mon@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
            send_weekday=0,
            confirmed_at=self.monday - timedelta(days=2),
        )

        dry_out = StringIO()
        with patch("django.utils.timezone.now", return_value=self.monday):
            call_command("send_weekly_digest", "--dry-run", stdout=dry_out)
        text = dry_out.getvalue()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(DigestIssue.objects.count(), 0)
        self.assertEqual(DigestDelivery.objects.count(), 0)
        unassigned.refresh_from_db()
        self.assertIsNone(unassigned.send_weekday)
        self.assertIn("Weekday: 0 (Monday)", text)
        self.assertIn("Overdue: 1", text)
        self.assertIn("Due today: 1", text)
        self.assertIn("Would send (after daily cap): 1", text)
        self.assertIn("Left over: 1", text)

        # Skip when empty creates no rows
        Story.objects.update(site_published_date=self.monday.date() - timedelta(days=30))
        empty_out = StringIO()
        with patch("django.utils.timezone.now", return_value=self.monday):
            call_command("send_weekly_digest", stdout=empty_out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(DigestIssue.objects.count(), 0)
        self.assertEqual(DigestDelivery.objects.count(), 0)
        self.assertIn("nothing new", empty_out.getvalue())

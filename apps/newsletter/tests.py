from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.users.models import User

from .models import NewsletterSubscription


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

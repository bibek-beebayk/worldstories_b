"""API-level tests: auth scoping, filtering/sorting, and dashboard maths."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.social_media_analytics.models import (
    AccountSnapshot,
    ContentItem,
    ContentType,
    MetricSnapshot,
    Platform,
    PlatformConnection,
)

TEST_KEY = "0zLQ8kQyVv3sJ9r0Kf5fH0aWq2Xn9Zt8sJmM4pQeR1c="
BASE = "/api/social-media-analytics"


def auth_client(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


@override_settings(SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=TEST_KEY)
class ApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="owner", email="owner@example.invalid", password="pw"
        )
        self.other = User.objects.create_user(
            username="stranger", email="stranger@example.invalid", password="pw"
        )
        self.client = auth_client(self.user)
        self.now = timezone.now()

        self.connection = PlatformConnection.objects.create(
            user=self.user,
            platform=Platform.YOUTUBE,
            external_account_id="yt-1",
            display_name="My Channel",
            access_token="t",
        )
        AccountSnapshot.objects.create(
            platform_connection=self.connection,
            captured_at=self.now - timedelta(days=14),
            follower_count=1000,
        )
        AccountSnapshot.objects.create(
            platform_connection=self.connection,
            captured_at=self.now,
            follower_count=1200,
        )

        self.item = ContentItem.objects.create(
            platform_connection=self.connection,
            platform_content_id="v1",
            content_type=ContentType.VIDEO,
            caption_or_title="First video",
            published_at=self.now - timedelta(days=20),
        )
        # A cumulative counter growing over three sync runs.
        for offset, views in ((16, 100), (8, 400), (0, 900)):
            MetricSnapshot.objects.create(
                content_item=self.item,
                captured_at=self.now - timedelta(days=offset),
                views=views,
                likes=views // 10,
            )

    def test_health_is_public(self):
        response = self.client.get(f"{BASE}/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(APIClient().get(f"{BASE}/health/").status_code, 200)

    def test_endpoints_require_authentication(self):
        anon = APIClient()
        for path in ("/connections/", "/content/", "/dashboard/summary/"):
            self.assertEqual(anon.get(f"{BASE}{path}").status_code, 401, path)

    def test_content_is_scoped_to_the_requesting_user(self):
        response = auth_client(self.other).get(f"{BASE}/content/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_connections_returns_all_four_platforms(self):
        response = self.client.get(f"{BASE}/connections/")
        rows = response.json()
        self.assertEqual(len(rows), 4)
        by_platform = {row["platform"]: row for row in rows}
        self.assertTrue(by_platform["youtube"]["connected"])
        self.assertFalse(by_platform["tiktok"]["connected"])
        # Follower count is denormalised from the newest account snapshot.
        self.assertEqual(by_platform["youtube"]["connection"]["follower_count"], 1200)

    def test_content_list_carries_the_latest_snapshot_only(self):
        response = self.client.get(f"{BASE}/content/")
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["latest_metrics"]["views"], 900)

    def test_history_returns_the_full_series_oldest_first(self):
        response = self.client.get(f"{BASE}/content/{self.item.pk}/history/")
        history = response.json()["history"]
        self.assertEqual([row["views"] for row in history], [100, 400, 900])

    def test_dashboard_views_are_a_period_delta_not_a_lifetime_total(self):
        response = self.client.get(f"{BASE}/dashboard/summary/?days=7")
        data = response.json()
        youtube = next(p for p in data["platforms"] if p["platform"] == "youtube")

        # Lifetime views are 900, but only 500 of them were earned in the last
        # 7 days (400 -> 900). Reporting 900 here would double-count the
        # views the post already had.
        self.assertEqual(youtube["views_this_period"], 500)
        self.assertEqual(youtube["follower_count"], 1200)

    def test_dashboard_marks_unconnected_platforms_as_null_not_zero(self):
        data = self.client.get(f"{BASE}/dashboard/summary/").json()
        tiktok = next(p for p in data["platforms"] if p["platform"] == "tiktok")
        self.assertFalse(tiktok["connected"])
        self.assertIsNone(tiktok["follower_count"])

    def test_content_filters_by_platform(self):
        response = self.client.get(f"{BASE}/content/?platform=tiktok")
        self.assertEqual(response.json()["results"], [])
        response = self.client.get(f"{BASE}/content/?platform=youtube")
        self.assertEqual(len(response.json()["results"]), 1)

    def test_content_sort_key_is_whitelisted(self):
        # An unknown sort falls back to the default rather than erroring or
        # reaching the ORM.
        response = self.client.get(f"{BASE}/content/?sort=;DROP TABLE")
        self.assertEqual(response.status_code, 200)

    def test_account_trends_returns_the_series(self):
        response = self.client.get(f"{BASE}/dashboard/account-trends/?platform=youtube&days=30")
        series = response.json()
        self.assertEqual(len(series), 1)
        self.assertEqual([p["follower_count"] for p in series[0]["points"]], [1000, 1200])

    def test_disconnect_clears_credentials_but_keeps_history(self):
        response = self.client.post(f"{BASE}/connections/youtube/disconnect/")
        self.assertEqual(response.status_code, 200)

        self.connection.refresh_from_db()
        self.assertFalse(self.connection.is_active)
        self.assertEqual(self.connection.access_token, "")
        # Analytics already collected survive a disconnect.
        self.assertEqual(ContentItem.objects.count(), 1)
        self.assertEqual(MetricSnapshot.objects.count(), 3)

    def test_disconnect_rejects_an_unknown_platform(self):
        response = self.client.post(f"{BASE}/connections/myspace/disconnect/")
        self.assertEqual(response.status_code, 400)

    # Django's default mail_admins handler fires on any 5xx once DEBUG is
    # False (as it is under the test runner) and renders the debug traceback
    # template, which trips a copy() incompatibility in Django 5.0 on Python
    # 3.14. Unrelated to this endpoint — muted so the assertion can run.
    @mock.patch("django.utils.log.AdminEmailHandler.emit")
    def test_oauth_url_reports_missing_configuration_as_503(self, _emit):
        with override_settings(META_APP_ID="", META_APP_SECRET=""):
            response = self.client.get(f"{BASE}/connections/instagram/oauth-url/")
        self.assertEqual(response.status_code, 503)

    def test_login_returns_a_token_pair(self):
        # NB: this project's User model sets USERNAME_FIELD = "email", so
        # simplejwt's login serializer expects "email", not "username".
        response = APIClient().post(
            f"{BASE}/auth/login/",
            {"email": "owner@example.invalid", "password": "pw"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())
        self.assertIn("refresh", response.json())

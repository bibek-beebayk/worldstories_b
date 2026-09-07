"""Tests for the sync pipeline and the aggregation the dashboard depends on.

Platform HTTP is stubbed at the integration-module boundary — these tests are
about our own snapshot/aggregation logic, which is where the subtle bugs live
(cumulative counters vs. period deltas, per-platform null handling, one
platform's failure not sinking another's).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.social_media_analytics import services
from apps.social_media_analytics.integrations.base import (
    AccountMetrics,
    FetchedContent,
    FetchedMetrics,
    IntegrationError,
)
from apps.social_media_analytics.models import (
    AccountSnapshot,
    ContentItem,
    ContentType,
    MetricSnapshot,
    Platform,
    PlatformConnection,
    SyncRun,
)

# A valid Fernet key so the encrypted fields work in tests without depending
# on the developer's .env.
TEST_KEY = "0zLQ8kQyVv3sJ9r0Kf5fH0aWq2Xn9Zt8sJmM4pQeR1c="


def make_user(username="analytics-tester"):
    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.invalid", password="pw"
    )


def fake_module(*, account=None, contents=None, error=None):
    """A stand-in for an ``integrations.<platform>`` module."""

    def fetch_account(connection):
        if error:
            raise error
        return account or AccountMetrics(follower_count=100)

    def fetch_content(connection):
        if error:
            raise error
        return list(contents or [])

    return SimpleNamespace(
        fetch_account=fetch_account,
        fetch_content=fetch_content,
        refresh=None,
    )


@override_settings(SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=TEST_KEY)
class SyncConnectionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = PlatformConnection.objects.create(
            user=self.user,
            platform=Platform.INSTAGRAM,
            external_account_id="ig-1",
            display_name="test.account",
            access_token="token-value",
        )

    def test_sync_appends_snapshots_and_never_overwrites(self):
        content = FetchedContent(
            platform_content_id="media-1",
            content_type=ContentType.REEL,
            caption_or_title="A reel",
            metrics=FetchedMetrics(views=100, likes=10, saves=2),
        )
        module = fake_module(
            account=AccountMetrics(follower_count=500), contents=[content]
        )

        with mock.patch.object(services.integrations, "get_integration", return_value=module):
            services.sync_connection(self.connection)
            # Second run: the counter has grown.
            content.metrics = FetchedMetrics(views=180, likes=15, saves=3)
            services.sync_connection(self.connection)

        item = ContentItem.objects.get(platform_content_id="media-1")
        snapshots = list(item.snapshots.order_by("captured_at"))

        # One ContentItem, two snapshots — history accumulates, nothing is
        # rewritten in place.
        self.assertEqual(ContentItem.objects.count(), 1)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([s.views for s in snapshots], [100, 180])
        self.assertEqual(AccountSnapshot.objects.count(), 2)

    def test_unreported_metrics_stay_null(self):
        module = fake_module(
            contents=[
                FetchedContent(
                    platform_content_id="media-2",
                    content_type=ContentType.POST,
                    metrics=FetchedMetrics(views=10, likes=1),
                )
            ]
        )
        with mock.patch.object(services.integrations, "get_integration", return_value=module):
            services.sync_connection(self.connection)

        snapshot = MetricSnapshot.objects.get()
        self.assertEqual(snapshot.views, 10)
        # Never coerced to 0 — null means "this platform does not report it".
        self.assertIsNone(snapshot.shares)
        self.assertIsNone(snapshot.impressions)
        self.assertIsNone(snapshot.reach)

    def test_sync_marks_the_connection_synced(self):
        module = fake_module()
        with mock.patch.object(services.integrations, "get_integration", return_value=module):
            services.sync_connection(self.connection)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.last_synced_at)
        self.assertEqual(self.connection.last_sync_status, SyncRun.Status.SUCCESS)


@override_settings(SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=TEST_KEY)
class SyncPlatformIsolationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.good = PlatformConnection.objects.create(
            user=self.user,
            platform=Platform.YOUTUBE,
            external_account_id="yt-good",
            access_token="t",
        )
        self.bad = PlatformConnection.objects.create(
            user=self.user,
            platform=Platform.YOUTUBE,
            external_account_id="yt-bad",
            access_token="t",
        )

    def test_one_failing_connection_does_not_sink_the_others(self):
        def get_integration(platform):
            return fake_module()

        def sync_connection(connection):
            if connection.external_account_id == "yt-bad":
                raise IntegrationError("boom")
            return (1, 2)

        with mock.patch.object(services, "sync_connection", side_effect=sync_connection):
            run = services.sync_platform(Platform.YOUTUBE)

        # The good connection's data still landed, and the run reports the
        # failure rather than swallowing or propagating it.
        self.assertEqual(run.status, SyncRun.Status.PARTIAL)
        self.assertIn("yt-bad", run.message)
        self.assertEqual(run.items_synced, 1)

        self.bad.refresh_from_db()
        self.assertEqual(self.bad.last_sync_status, SyncRun.Status.FAILED)

    def test_no_connections_is_a_success_not_a_failure(self):
        PlatformConnection.objects.all().delete()
        run = services.sync_platform(Platform.TIKTOK)
        self.assertEqual(run.status, SyncRun.Status.SUCCESS)


@override_settings(SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=TEST_KEY)
class EncryptedTokenTests(TestCase):
    def test_token_is_ciphertext_in_the_database(self):
        user = make_user()
        connection = PlatformConnection.objects.create(
            user=user,
            platform=Platform.TIKTOK,
            external_account_id="tt-1",
            access_token="super-secret-token",
        )

        # Reading through the ORM decrypts transparently…
        connection.refresh_from_db()
        self.assertEqual(connection.access_token, "super-secret-token")

        # …but the raw column holds a Fernet token, not the plaintext.
        from django.db import connection as db

        with db.cursor() as cursor:
            cursor.execute(
                "SELECT access_token FROM social_media_analytics_platformconnection "
                "WHERE id = %s",
                [connection.pk],
            )
            raw = cursor.fetchone()[0]
        self.assertNotIn("super-secret-token", raw)
        self.assertTrue(raw.startswith("gAAAAA"))


@override_settings(SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=TEST_KEY)
class TokenRefreshTests(TestCase):
    def test_expiring_token_is_refreshed_before_a_sync(self):
        user = make_user()
        connection = PlatformConnection.objects.create(
            user=user,
            platform=Platform.TIKTOK,
            external_account_id="tt-2",
            access_token="old",
            refresh_token="old-refresh",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        module = fake_module()
        module.refresh = lambda conn: SimpleNamespace(
            access_token="new",
            refresh_token="new-refresh",
            expires_at=timezone.now() + timedelta(days=1),
        )

        with mock.patch.object(services.integrations, "get_integration", return_value=module):
            services.ensure_fresh_token(connection)

        connection.refresh_from_db()
        self.assertEqual(connection.access_token, "new")
        # TikTok rotates refresh tokens; the new one must replace the old.
        self.assertEqual(connection.refresh_token, "new-refresh")

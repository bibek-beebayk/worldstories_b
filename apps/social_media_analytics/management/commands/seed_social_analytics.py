"""Seed realistic mock analytics data for all four platforms.

Why this exists: the platform developer apps (Meta, Google, TikTok) each need
their own review/approval before real credentials can be issued, so the
OAuth-connected path cannot be exercised end to end during development. This
command populates the same tables the sync tasks write to, which makes the
whole client flow — dashboard → content list → content detail → trend chart —
runnable against a local backend with no third-party credentials at all.

It writes *only* through the real models, so nothing here is a special case
the API has to know about.

    python manage.py seed_social_analytics --user <username> --days 60

Re-running replaces the mock data (it is keyed by a ``mock-`` content id
prefix) and never touches real synced rows.
"""

from __future__ import annotations

import math
import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.social_media_analytics.models import (
    AccountSnapshot,
    ContentItem,
    ContentType,
    MetricSnapshot,
    Platform,
    PlatformConnection,
    SyncRun,
)

MOCK_PREFIX = "mock-"

# Per-platform shape of the fake account: starting followers, daily growth
# rate, and which metric columns that platform actually reports (mirrors the
# availability table in models.MetricSnapshot, so the seeded data has the
# same nulls real data would).
PLATFORM_PROFILE = {
    Platform.FACEBOOK: {
        "handle": "WorldStories",
        "followers": 12_400,
        "growth": 18,
        "types": [ContentType.POST],
        "fields": {"impressions", "reach", "likes", "comments", "shares"},
        "lifetime_views": False,
    },
    Platform.INSTAGRAM: {
        "handle": "worldstories.net",
        "followers": 28_900,
        "growth": 55,
        "types": [ContentType.POST, ContentType.REEL],
        "fields": {"views", "reach", "likes", "comments", "saves"},
        "lifetime_views": False,
    },
    Platform.TIKTOK: {
        "handle": "@worldstories",
        "followers": 46_200,
        "growth": 130,
        "types": [ContentType.VIDEO],
        "fields": {"views", "likes", "comments", "shares"},
        "lifetime_views": False,
    },
    Platform.YOUTUBE: {
        "handle": "World Stories",
        "followers": 9_150,
        "growth": 12,
        "types": [ContentType.VIDEO, ContentType.REEL],
        "fields": {"views", "likes", "comments"},
        "lifetime_views": True,
    },
}

TITLES = [
    "The Fisherman and His Wife — read along",
    "Anansi and the Pot of Wisdom",
    "Why the Sea is Salt (Norwegian folktale)",
    "The Boy Who Drew Cats — full narration",
    "Baba Yaga's Hut: 3 versions compared",
    "How to read aloud to a 4-year-old",
    "The Nightingale — Hans Christian Andersen",
    "Momotarō, the Peach Boy",
    "5 folktales from West Africa you've never heard",
    "The Snow Queen, part one",
    "Behind the scenes: recording an audiobook",
    "The Tortoise and the Birds — Igbo tale",
    "Rip Van Winkle in 60 seconds",
    "Storytime: The Little Match Girl",
    "Reading with expression — three techniques",
]


class Command(BaseCommand):
    help = "Populate mock social-media analytics data for local development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            help="Username to attach the connections to (defaults to the first superuser).",
        )
        parser.add_argument(
            "--days", type=int, default=60, help="How many days of history to generate."
        )
        parser.add_argument(
            "--items-per-platform", type=int, default=12, help="Content items per platform."
        )
        parser.add_argument(
            "--clear", action="store_true", help="Remove existing mock data and exit."
        )
        parser.add_argument("--seed", type=int, default=20240917, help="RNG seed.")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options.get("user")
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist as exc:
                raise CommandError(f"No user named {username!r}") from exc
        else:
            user = User.objects.filter(is_superuser=True).order_by("id").first()
            if user is None:
                raise CommandError(
                    "No superuser found — pass --user, or create one with "
                    "`manage.py createsuperuser`."
                )

        if options["clear"]:
            removed = self._clear(user)
            self.stdout.write(self.style.SUCCESS(f"Removed {removed} mock connection(s)."))
            return

        rng = random.Random(options["seed"])
        days = max(7, options["days"])
        per_platform = max(1, options["items_per_platform"])

        self._clear(user)
        now = timezone.now()

        with transaction.atomic():
            for platform, profile in PLATFORM_PROFILE.items():
                connection = self._make_connection(user, platform, profile, now)
                self._make_account_history(connection, profile, days, now, rng)
                self._make_content(connection, profile, per_platform, days, now, rng)
                SyncRun.objects.create(
                    platform=platform,
                    started_at=now - timedelta(minutes=4),
                    finished_at=now - timedelta(minutes=3),
                    status=SyncRun.Status.SUCCESS,
                    items_synced=per_platform,
                    snapshots_written=per_platform * days + days,
                    message="Seeded by manage.py seed_social_analytics",
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(PLATFORM_PROFILE)} platforms × {per_platform} items × "
                f"{days} days of snapshots for {user}."
            )
        )

    # -- helpers ---------------------------------------------------------

    def _clear(self, user) -> int:
        qs = PlatformConnection.objects.filter(
            user=user, external_account_id__startswith=MOCK_PREFIX
        )
        count = qs.count()
        # Cascades take the content items and every snapshot with them.
        qs.delete()
        SyncRun.objects.filter(message__startswith="Seeded by").delete()
        return count

    def _make_connection(self, user, platform, profile, now) -> PlatformConnection:
        return PlatformConnection.objects.create(
            user=user,
            platform=platform,
            external_account_id=f"{MOCK_PREFIX}{platform}-account",
            display_name=profile["handle"],
            # A placeholder, not a credential — it exists so the connections
            # screen shows "connected". The integrations would reject it.
            access_token="seeded-placeholder-not-a-real-token",
            refresh_token="",
            token_expires_at=now + timedelta(days=59),
            connected_at=now - timedelta(days=120),
            last_synced_at=now - timedelta(minutes=3),
            last_sync_status=SyncRun.Status.SUCCESS,
            is_active=True,
            extra={"seeded": True},
        )

    def _make_account_history(self, connection, profile, days, now, rng):
        followers = profile["followers"] - profile["growth"] * days
        lifetime = rng.randint(200_000, 900_000) if profile["lifetime_views"] else None
        rows = []
        for offset in range(days, -1, -1):
            captured = now - timedelta(days=offset)
            # Growth with a little noise, plus a weekend bump.
            noise = rng.randint(-profile["growth"] // 3, profile["growth"])
            followers = max(0, followers + profile["growth"] + noise)
            if lifetime is not None:
                lifetime += rng.randint(400, 3_000)
            rows.append(
                AccountSnapshot(
                    platform_connection=connection,
                    captured_at=captured,
                    follower_count=followers,
                    total_views_lifetime=lifetime,
                )
            )
        AccountSnapshot.objects.bulk_create(rows, batch_size=500)

    def _make_content(self, connection, profile, count, days, now, rng):
        fields = profile["fields"]
        for index in range(count):
            published = now - timedelta(
                days=rng.randint(1, days - 1), hours=rng.randint(0, 23)
            )
            content_type = rng.choice(profile["types"])
            title = TITLES[(index + hash(connection.platform)) % len(TITLES)]
            item = ContentItem.objects.create(
                platform_connection=connection,
                platform_content_id=f"{MOCK_PREFIX}{connection.platform}-{index:03d}",
                content_type=content_type,
                caption_or_title=title,
                permalink=f"https://example.invalid/{connection.platform}/{index:03d}",
                # picsum.photos is a stable public placeholder image host; the
                # seed keeps the same item showing the same picture.
                thumbnail_url=f"https://picsum.photos/seed/{connection.platform}{index}/400/400",
                published_at=published,
            )

            # A post's audience decays: most of its lifetime views land in the
            # first couple of days, then it trickles. Modelled as a saturating
            # curve so the trend chart looks like a real one.
            ceiling = rng.randint(2_000, 90_000) * (3 if content_type == ContentType.REEL else 1)
            like_rate = rng.uniform(0.03, 0.12)
            comment_rate = rng.uniform(0.001, 0.01)
            share_rate = rng.uniform(0.002, 0.02)
            save_rate = rng.uniform(0.005, 0.03)

            rows = []
            day = published
            while day <= now:
                age = max(0.0, (day - published).total_seconds() / 86400)
                # 1 - e^(-age/tau): fast early, flattening later.
                progress = 1 - math.exp(-age / 2.2)
                views = int(ceiling * progress * rng.uniform(0.97, 1.0))
                likes = int(views * like_rate)
                comments = int(views * comment_rate)
                shares = int(views * share_rate)
                saves = int(views * save_rate)
                rows.append(
                    MetricSnapshot(
                        content_item=item,
                        captured_at=day,
                        views=views if "views" in fields else None,
                        likes=likes if "likes" in fields else None,
                        comments=comments if "comments" in fields else None,
                        shares=shares if "shares" in fields else None,
                        saves=saves if "saves" in fields else None,
                        # Impressions run above reach (same person, repeat views).
                        impressions=int(views * 1.35) if "impressions" in fields else None,
                        reach=int(views * 0.82) if "reach" in fields else None,
                    )
                )
                day += timedelta(days=1)
            MetricSnapshot.objects.bulk_create(rows, batch_size=500)

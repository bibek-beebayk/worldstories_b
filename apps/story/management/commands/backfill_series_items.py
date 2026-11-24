"""Backfill AudiobookSeriesItem rows from legacy Audiobook.series / series_order.

Idempotent, chunked in batches of 500 inside transactions, with --dry-run.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max

from apps.story.models import Audiobook, AudiobookSeriesItem

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Copy legacy Audiobook.series memberships into AudiobookSeriesItem rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show created/skipped counts without modifying the database.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        created = 0
        skipped = 0

        ids = list(
            Audiobook.objects.filter(series__isnull=False)
            .order_by("id")
            .values_list("id", flat=True)
        )

        # Track per-series next order across chunks for dry-run accuracy too.
        max_order_by_series: dict[int, int] = dict(
            AudiobookSeriesItem.objects.values("series_id")
            .annotate(m=Max("order"))
            .values_list("series_id", "m")
        )

        for start in range(0, len(ids), BATCH_SIZE):
            chunk_ids = ids[start : start + BATCH_SIZE]
            with transaction.atomic():
                audiobooks = list(
                    Audiobook.objects.filter(pk__in=chunk_ids)
                    .only("id", "series_id", "series_order")
                    .order_by("id")
                )
                existing_pairs = set(
                    AudiobookSeriesItem.objects.filter(audiobook_id__in=chunk_ids).values_list(
                        "series_id", "audiobook_id"
                    )
                )
                for ab in audiobooks:
                    if not ab.series_id:
                        continue
                    pair = (ab.series_id, ab.id)
                    if pair in existing_pairs:
                        skipped += 1
                        continue
                    if ab.series_order and ab.series_order > 0:
                        item_order = ab.series_order
                    else:
                        item_order = max_order_by_series.get(ab.series_id, 0) + 1
                    max_order_by_series[ab.series_id] = max(
                        max_order_by_series.get(ab.series_id, 0), item_order
                    )
                    created += 1
                    if not dry_run:
                        AudiobookSeriesItem.objects.create(
                            series_id=ab.series_id,
                            audiobook_id=ab.id,
                            order=item_order,
                        )

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}backfill_series_items complete: created={created}, skipped={skipped}"
            )
        )

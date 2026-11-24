"""Recompute cached total_duration_seconds and track_count on all Audiobooks.

Idempotent, chunked in batches of 500 inside transactions, with --dry-run.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.story.management.commands.audit_audiobooks import (
    _totals_differ,
    compute_expected_audiobook_totals,
)
from apps.story.models import Audiobook

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Recompute total_duration_seconds and track_count for all Audiobooks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many audiobooks would change without writing.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        changed = 0
        total = 0

        ids = list(Audiobook.objects.order_by("id").values_list("id", flat=True))
        for start in range(0, len(ids), BATCH_SIZE):
            chunk_ids = ids[start : start + BATCH_SIZE]
            with transaction.atomic():
                audiobooks = list(Audiobook.objects.filter(pk__in=chunk_ids).order_by("id"))
                for ab in audiobooks:
                    total += 1
                    expected_count, expected_total = compute_expected_audiobook_totals(ab)
                    if ab.track_count != expected_count or _totals_differ(
                        ab.total_duration_seconds, expected_total
                    ):
                        changed += 1
                        if not dry_run:
                            ab.refresh_cache()

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}recompute_audiobook_totals complete: checked={total}, changed={changed}"
            )
        )

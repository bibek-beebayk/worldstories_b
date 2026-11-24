"""Legacy backfill command retained as a safe no-op.

The legacy Audiobook.series and Audiobook.series_order columns have been dropped
after migrating all series memberships to AudiobookSeriesItem.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "No-op: legacy Audiobook.series / series_order columns have been dropped."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="No-op flag retained for backwards compatibility.",
        )

    def handle(self, *args, **options):
        prefix = "[DRY RUN] " if options.get("dry_run") else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}backfill_series_items: legacy Audiobook.series columns have been dropped; nothing to do."
            )
        )

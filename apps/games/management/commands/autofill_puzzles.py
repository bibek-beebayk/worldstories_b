"""Auto-fill upcoming Name That Story daily puzzles."""

from datetime import timedelta

from django.core.management.base import BaseCommand

from apps.games.services import auto_fill, utc_today


class Command(BaseCommand):
    help = "Auto-fill upcoming Name That Story daily puzzles starting from today."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Number of days to fill starting from today UTC (default: 14).",
        )
        parser.add_argument(
            "--from-tomorrow",
            action="store_true",
            help="Start filling from tomorrow UTC instead of today.",
        )

    def handle(self, *args, **options):
        days = max(1, int(options.get("days") or 14))
        start = utc_today() + (timedelta(days=1) if options.get("from_tomorrow") else timedelta(days=0))
        result = auto_fill(start, days)
        self.stdout.write(
            self.style.SUCCESS(
                f"autofill_puzzles complete: created={len(result['created'])}, skipped={len(result['skipped'])}"
            )
        )

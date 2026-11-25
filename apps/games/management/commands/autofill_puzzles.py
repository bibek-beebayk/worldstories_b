from django.core.management.base import BaseCommand

from apps.games.services import auto_fill, utc_today


class Command(BaseCommand):
    help = "Auto-fill upcoming Name That Story daily puzzles using the weekly rotation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Number of days starting from today (UTC) to fill (default: 14).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        result = auto_fill(utc_today(), days=days)
        self.stdout.write(
            self.style.SUCCESS(
                f"Auto-fill complete: {result['created_count']} approved, "
                f"{result['drafted_count']} drafted, "
                f"{result['skipped_empty_count']} skipped/empty."
            )
        )

from django.core.management.base import BaseCommand

from apps.games.crossword import auto_generate
from apps.games.services.engine import utc_today


class Command(BaseCommand):
    help = "Auto-generate upcoming draft 5x5 Mini Crossword grids."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Number of days starting from today (UTC) to generate (default: 14).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        res = auto_generate(utc_today(), days=days)
        self.stdout.write(
            self.style.SUCCESS(
                f"Auto-generate complete: {res['created_count']} draft(s) created, "
                f"{res['skipped_count']} skipped."
            )
        )

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.games.api_mini import import_clue_bank_csv_text


class Command(BaseCommand):
    help = "Import clues into the Mini Crossword ClueBank from a CSV file (answer,clue,story_slug)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to CSV file.")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        res = import_clue_bank_csv_text(path.read_text(encoding="utf-8"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Clue bank import complete: {res['imported']} imported, "
                f"{res['updated']} updated, {res['skipped']} skipped."
            )
        )

import csv
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.games.models import ClueBank


class Command(BaseCommand):
    help = "Export the Mini Crossword ClueBank to a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("output_path", type=str, help="Destination CSV path.")
        parser.add_argument(
            "--include-usage",
            action="store_true",
            default=False,
            help="Include the times_used column in the exported CSV.",
        )

    def handle(self, *args, **options):
        path = Path(options["output_path"])
        include_usage = bool(options["include_usage"])
        path.parent.mkdir(parents=True, exist_ok=True)

        qs = ClueBank.objects.select_related("story").order_by("answer", "clue", "id")
        header = ["answer", "clue", "story_slug", "is_name"]
        if include_usage:
            header.append("times_used")

        written = 0
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(header)
            for row in qs.iterator(chunk_size=500):
                fields = [
                    row.answer,
                    row.clue,
                    row.story.slug if row.story_id else "",
                    "true" if row.is_name else "false",
                ]
                if include_usage:
                    fields.append(str(row.times_used))
                writer.writerow(fields)
                written += 1

        self.stdout.write(
            self.style.SUCCESS(f"Exported {written} clue bank row(s) to {path}.")
        )

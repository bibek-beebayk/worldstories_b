from collections import Counter

from django.core.management.base import BaseCommand

from apps.games.crossword import apply_difficulty_to_puzzle
from apps.games.models import MiniCrossword


class Command(BaseCommand):
    help = "Recompute difficulty and difficulty_score for MiniCrossword puzzles without difficulty_override."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute and report counts without writing changes to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        qs = MiniCrossword.objects.filter(difficulty_override=False).order_by("date")
        counts: Counter[str] = Counter()
        updated = 0

        for puzzle in qs:
            breakdown = apply_difficulty_to_puzzle(puzzle)
            if breakdown is None:
                continue
            counts[breakdown["label"]] += 1
            if not dry_run:
                puzzle.save(update_fields=["difficulty", "difficulty_score", "updated_at"])
                updated += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Recomputed {sum(counts.values())} puzzle(s) "
                f"(updated={updated}): easy={counts['easy']}, "
                f"medium={counts['medium']}, hard={counts['hard']}."
            )
        )

from django.core.management.base import BaseCommand

from apps.games.crossword import auto_generate
from apps.games.services.engine import utc_today


class Command(BaseCommand):
    help = "Auto-generate upcoming draft 5x5 Mini Crossword grids using only ClueBank answers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Number of days starting from today (UTC) to fill (default: 14).",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=None,
            help="Exact number of new draft grids to generate (overrides --days).",
        )
        parser.add_argument(
            "--seed",
            type=str,
            default=None,
            help="Optional RNG seed for reproducible generation.",
        )
        parser.add_argument(
            "--per-pattern-timeout",
            type=float,
            default=5.0,
            help="Max seconds spent per pattern attempt (default: 5.0).",
        )
        parser.add_argument(
            "--total-timeout",
            type=float,
            default=60.0,
            help="Max total seconds for the entire run (default: 60.0).",
        )

    def handle(self, *args, **options):
        res = auto_generate(
            utc_today(),
            days=options["days"],
            count=options["count"],
            seed=options["seed"],
            per_pattern_timeout=options["per_pattern_timeout"],
            total_timeout=options["total_timeout"],
        )

        if res.get("reason") == "bank_too_small":
            bank = res["bank"]
            by_len = bank["by_length"]
            scarce = ", ".join(item["letter"] for item in bank.get("scarce_letters", []))
            self.stdout.write(
                self.style.WARNING(
                    f"Auto-generate produced 0 of {res['requested']} grid(s) "
                    f"(reason=bank_too_small, elapsed={res['elapsed_seconds']}s). "
                    f"Bank answers: {bank['total_answers']} "
                    f"(3-letter: {by_len['3']}, 4-letter: {by_len['4']}, 5-letter: {by_len['5']}). "
                    f"Scarce letters: {scarce or 'none'}. "
                    f"{res['suggestion']}"
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Auto-generate complete: {res['created_count']} of {res['requested']} draft(s) "
                f"created, {res['skipped_count']} skipped in {res['elapsed_seconds']}s."
            )
        )

from django.core.management.base import BaseCommand

from apps.story.models import Story
from apps.story.signals import recompute_chapter_reading_minutes


class Command(BaseCommand):
    help = (
        "Computes and stores cached_chapter_reading_minutes for existing Story rows "
        "whose chapters predate the signal that now keeps the value current. "
        "Reads only content already in the database — no remote storage access, "
        "unlike backfill_file_reading_times."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Recompute every story, not only the ones with no cached value yet.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        verbose = options["verbosity"] >= 1
        queryset = Story.objects.filter(chapters__isnull=False).distinct()
        if not options["all"]:
            queryset = queryset.filter(cached_chapter_reading_minutes__isnull=True)

        updated = 0
        for story_id, slug, current in queryset.values_list(
            "id", "slug", "cached_chapter_reading_minutes"
        ):
            if options["dry_run"]:
                if verbose:
                    self.stdout.write(f"{slug}: would recompute (currently {current})")
                updated += 1
                continue
            minutes = recompute_chapter_reading_minutes(story_id)
            if verbose and minutes != current:
                self.stdout.write(f"{slug}: {current} -> {minutes}")
            updated += 1

        if verbose:
            verb = "Would update" if options["dry_run"] else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{verb} {updated} stories."))

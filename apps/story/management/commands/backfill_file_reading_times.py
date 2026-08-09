from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.story.models import Story
from apps.story import reading_time


class Command(BaseCommand):
    help = (
        "Probes and stores cached_file_reading_minutes for existing Story rows "
        "that have an epub/pdf file but no cached estimate — stories that predate "
        "the reading-time caching fix (new uploads already get this set "
        "automatically at upload time). Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-probe every story with an epub/pdf file, not just ones missing a cached estimate.",
        )

    def handle(self, *args, **options):
        queryset = Story.objects.filter(Q(epub_file__gt="") | Q(pdf_file__gt=""))
        if not options["all"]:
            queryset = queryset.filter(cached_file_reading_minutes__isnull=True)
        total = queryset.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to backfill."))
            return

        succeeded = 0
        failed = []
        for story in queryset.iterator():
            # Same priority as story_reading_minutes: epub wins over pdf.
            if story.epub_file:
                minutes = reading_time.epub_reading_minutes(story.epub_file)
            elif story.pdf_file:
                minutes = reading_time.pdf_reading_minutes(story.pdf_file)
            else:
                continue

            if minutes is None:
                failed.append(story)
                continue

            story.cached_file_reading_minutes = minutes
            story.save(update_fields=["cached_file_reading_minutes"])
            succeeded += 1
            self.stdout.write(f"  [{story.slug}] {minutes} min")

        self.stdout.write(self.style.SUCCESS(f"Backfilled {succeeded}/{total} stories."))
        if failed:
            self.stdout.write(self.style.WARNING(f"Could not probe {len(failed)} stor{'y' if len(failed) == 1 else 'ies'}:"))
            for story in failed:
                self.stdout.write(f"  [{story.slug}] (id={story.id})")

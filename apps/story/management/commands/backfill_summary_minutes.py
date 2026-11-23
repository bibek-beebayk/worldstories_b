"""Backfill Story.cached_summary_reading_minutes in batches of 500.

Safe to run multiple times: recomputes each story's summary reading estimate and
bulk-updates only rows whose cached value differs.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.story import reading_time
from apps.story.models import Story

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Backfill Story.cached_summary_reading_minutes for stories with a summary."

    def handle(self, *args, **options):
        queryset = (
            Story.objects.exclude(Q(summary__isnull=True) | Q(summary__exact=""))
            .only("id", "summary", "cached_summary_reading_minutes")
            .order_by("id")
        )
        updated = 0
        batch = []
        for story in queryset.iterator(chunk_size=BATCH_SIZE):
            minutes = reading_time.summary_reading_minutes(story.summary)
            if story.cached_summary_reading_minutes != minutes:
                story.cached_summary_reading_minutes = minutes
                batch.append(story)
                if len(batch) >= BATCH_SIZE:
                    Story.objects.bulk_update(batch, ["cached_summary_reading_minutes"])
                    updated += len(batch)
                    batch = []
        if batch:
            Story.objects.bulk_update(batch, ["cached_summary_reading_minutes"])
            updated += len(batch)

        self.stdout.write(
            self.style.SUCCESS(f"Updated cached_summary_reading_minutes on {updated} stories.")
        )

"""Backfill Audiobook rows for existing stories that have Audio tracks.

Idempotent and safe to run multiple times. Leaves title, author, genres, cover,
language and about blank on created Audiobooks so the linked Story's values are
inherited via effective_* helpers.
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.stats.models import AudioReadingProgress
from apps.story.models import Audio, Audiobook, Story
from apps.story.signals import unique_audiobook_slug

BATCH_SIZE = 200


class Command(BaseCommand):
    help = "Backfill Audiobook records, link Audio tracks and AudioReadingProgress rows, and refresh caches."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        stories_qs = (
            Story.objects.filter(Exists(Audio.objects.filter(story=OuterRef("pk"))))
            .only("id", "slug")
            .order_by("id")
        )

        created_count = 0
        tracks_attached = 0
        progress_updated = 0
        unknown_duration_count = 0
        reserved_slugs: set[str] = set()

        for story in stories_qs.iterator(chunk_size=BATCH_SIZE):
            audiobook = Audiobook.objects.filter(story_id=story.id).order_by("id").first()
            unlinked_tracks_qs = Audio.objects.filter(story_id=story.id, audiobook__isnull=True)
            unlinked_tracks_count = unlinked_tracks_qs.count()

            audio_ids = list(Audio.objects.filter(story_id=story.id).values_list("id", flat=True))
            unlinked_progress_qs = AudioReadingProgress.objects.filter(
                audio_id__in=audio_ids, audiobook__isnull=True
            )
            unlinked_progress_count = unlinked_progress_qs.count()

            if audiobook is None:
                slug = unique_audiobook_slug(story.slug, reserved=reserved_slugs)
                reserved_slugs.add(slug)
                created_count += 1
                if not dry_run:
                    audiobook = Audiobook.objects.create(
                        story_id=story.id,
                        title="",
                        slug=slug,
                    )

            tracks_attached += unlinked_tracks_count
            progress_updated += unlinked_progress_count

            if dry_run:
                durations = list(
                    Audio.objects.filter(story_id=story.id, audio_file__isnull=False)
                    .exclude(audio_file="")
                    .values_list("duration_seconds", flat=True)
                )
                if not durations or any(d is None for d in durations):
                    unknown_duration_count += 1
            else:
                if unlinked_tracks_count:
                    unlinked_tracks_qs.update(audiobook=audiobook)
                if unlinked_progress_count:
                    unlinked_progress_qs.update(audiobook=audiobook)
                audiobook.refresh_cache()
                if audiobook.total_duration_seconds is None:
                    unknown_duration_count += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Audiobooks created: {created_count}, "
                f"tracks attached: {tracks_attached}, "
                f"progress rows updated: {progress_updated}, "
                f"audiobooks with unknown total duration: {unknown_duration_count}"
            )
        )

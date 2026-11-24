"""Backfill AudioReadingProgress.audiobook from track.audiobook (or story's first audiobook).

Idempotent, chunked in batches of 500 inside transactions, with --dry-run.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.stats.models import AudioReadingProgress
from apps.story.models import Audiobook

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Move legacy AudioReadingProgress rows (audiobook IS NULL) onto audiobook keys."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show counts without modifying the database.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        moved = 0
        merged = 0
        skipped = 0

        ids = list(
            AudioReadingProgress.objects.filter(audiobook__isnull=True)
            .order_by("id")
            .values_list("id", flat=True)
        )

        for start in range(0, len(ids), BATCH_SIZE):
            chunk_ids = ids[start : start + BATCH_SIZE]
            with transaction.atomic():
                rows = list(
                    AudioReadingProgress.objects.filter(pk__in=chunk_ids)
                    .select_related("audio", "audio__audiobook")
                    .order_by("id")
                )
                for row in rows:
                    target_audiobook = None
                    if row.audio_id and row.audio.audiobook_id:
                        target_audiobook = row.audio.audiobook
                    elif row.story_id:
                        target_audiobook = (
                            Audiobook.objects.filter(story_id=row.story_id)
                            .order_by("id")
                            .first()
                        )

                    if target_audiobook is None:
                        skipped += 1
                        continue

                    # Check if a newer/existing audiobook-keyed row already exists for
                    # the same (user, audiobook, audio).
                    existing = (
                        AudioReadingProgress.objects.filter(
                            user_id=row.user_id,
                            audiobook_id=target_audiobook.id,
                            audio_id=row.audio_id,
                        )
                        .exclude(pk=row.pk)
                        .order_by("-updated_at")
                        .first()
                    )
                    if existing is not None:
                        merged += 1
                        if not dry_run:
                            if row.updated_at > existing.updated_at:
                                existing.delete()
                                AudioReadingProgress.objects.filter(pk=row.pk).update(
                                    audiobook_id=target_audiobook.id
                                )
                            else:
                                row.delete()
                    else:
                        moved += 1
                        if not dry_run:
                            AudioReadingProgress.objects.filter(pk=row.pk).update(
                                audiobook_id=target_audiobook.id
                            )

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}backfill_audio_progress complete: moved={moved}, merged={merged}, skipped={skipped}"
            )
        )

from django.core.management.base import BaseCommand

from apps.story.models import Audio
from apps.story import reading_time


class Command(BaseCommand):
    help = (
        "Probes and stores duration_seconds for existing Audio rows that "
        "predate the audiobook listening-time feature (new uploads already "
        "get this set automatically at upload time). Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-probe every audio row, not just ones missing a duration.",
        )

    def handle(self, *args, **options):
        queryset = Audio.objects.all() if options["all"] else Audio.objects.filter(
            duration_seconds__isnull=True
        )
        total = queryset.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to backfill."))
            return

        succeeded = 0
        failed = []
        for audio in queryset.iterator():
            duration = reading_time.probe_audio_duration_seconds(audio.audio_file)
            if duration is None:
                failed.append(audio)
                continue
            audio.duration_seconds = duration
            audio.save(update_fields=["duration_seconds"])
            succeeded += 1
            self.stdout.write(f"  [{audio.story.slug}] {audio.title}: {duration:.1f}s")

        self.stdout.write(self.style.SUCCESS(f"Backfilled {succeeded}/{total} audio chapters."))
        if failed:
            self.stdout.write(self.style.WARNING(f"Could not probe {len(failed)} file(s):"))
            for audio in failed:
                self.stdout.write(f"  [{audio.story.slug}] {audio.title} (id={audio.id})")

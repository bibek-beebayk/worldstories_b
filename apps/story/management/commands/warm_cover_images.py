from django.core.management.base import BaseCommand

from apps.story.models import Blog, Story
from core.libs.images import warm_bulk


class Command(BaseCommand):
    help = (
        "Generates and uploads the resized cover-image renditions (Story.SIZES/"
        "Blog.SIZES) for existing rows, so GENERATE_IMAGE_RENDITIONS_ON_REQUEST="
        "False (prod) serves an already-made small file instead of falling back "
        "to the full original upload. New saves are warmed automatically by the "
        "post_save signal in apps/story/models.py — this command is only for "
        "rows that predate that signal, or as a one-off backfill after adding a "
        "new size to SIZES. Safe to re-run: VersatileImageFieldWarmer skips "
        "renditions that already exist in storage."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            choices=["story", "blog", "all"],
            default="all",
            help="Which model's covers to warm (default: all).",
        )

    def handle(self, *args, **options):
        targets = []
        if options["model"] in ("story", "all"):
            targets.append(("Story", Story.objects.exclude(cover_image_file="")))
        if options["model"] in ("blog", "all"):
            targets.append(("Blog", Blog.objects.exclude(cover_image_file="")))

        for label, queryset in targets:
            total = queryset.count()
            if total == 0:
                self.stdout.write(f"{label}: nothing to warm.")
                continue
            self.stdout.write(f"{label}: warming {total} cover image(s)...")
            try:
                warm_bulk(queryset)
            except Exception as error:
                self.stdout.write(
                    self.style.ERROR(f"{label}: warming failed — {error}")
                )
                continue
            self.stdout.write(self.style.SUCCESS(f"{label}: done."))

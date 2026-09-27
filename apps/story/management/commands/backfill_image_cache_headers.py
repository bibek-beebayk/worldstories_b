from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

# Objects uploaded to R2 before AWS_S3_OBJECT_PARAMETERS (Cache-Control) was
# added carry no cache header at all, so they're re-fetched from R2/origin on
# every request instead of being served from a CDN edge cache. This rewrites
# those objects' metadata in place (an S3 CopyObject onto themselves — no
# content changes, no new upload) so old files get the same caching benefit
# new uploads get automatically. Covers both the original cover uploads and
# any already-warmed renditions (VersatileImageField nests renditions under
# the same prefix, e.g. "story_covers/__sized__/...").
COVER_PREFIXES = ["story_covers/", "blog_covers/"]


class Command(BaseCommand):
    help = (
        "Retroactively sets the Cache-Control header (from "
        "AWS_S3_OBJECT_PARAMETERS) on cover images already sitting in R2 from "
        "before that setting existed. Safe to re-run — re-copying an object "
        "that already has the header just rewrites the same metadata."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be updated without changing anything.",
        )

    def handle(self, *args, **options):
        cache_control = getattr(settings, "AWS_S3_OBJECT_PARAMETERS", {}).get(
            "CacheControl"
        )
        if not cache_control:
            self.stderr.write(
                self.style.ERROR(
                    "AWS_S3_OBJECT_PARAMETERS['CacheControl'] is not set — "
                    "nothing to backfill to."
                )
            )
            return

        bucket = getattr(default_storage, "bucket", None)
        if bucket is None:
            self.stderr.write(
                self.style.ERROR(
                    "default_storage has no S3 bucket — is DEFAULT_FILE_STORAGE "
                    "set to the S3/R2 backend in this environment?"
                )
            )
            return

        dry_run = options["dry_run"]
        total = 0
        updated = 0
        skipped = 0

        for prefix in COVER_PREFIXES:
            self.stdout.write(f"Scanning s3://{bucket.name}/{prefix} ...")
            for obj in bucket.objects.filter(Prefix=prefix):
                total += 1
                existing = obj.Object().cache_control
                if existing == cache_control:
                    skipped += 1
                    continue
                if dry_run:
                    self.stdout.write(f"  would update: {obj.key}")
                    continue
                source = obj.Object()
                extra_args = {
                    "CacheControl": cache_control,
                    "ContentType": source.content_type,
                    "MetadataDirective": "REPLACE",
                }
                bucket.copy(
                    {"Bucket": bucket.name, "Key": obj.key}, obj.key, ExtraArgs=extra_args
                )
                updated += 1
                self.stdout.write(f"  updated: {obj.key}")

        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {updated if not dry_run else total - skipped}/{total} object(s); "
                f"{skipped} already had the header."
            )
        )

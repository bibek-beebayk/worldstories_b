import os
import shutil
import subprocess
import tempfile

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand

# 22.05kHz MPEG-2 Layer III (a common encoding for older LibriVox "64kbps"
# releases) isn't decodable by every browser's media pipeline — this
# re-encodes anything below a safe sample rate threshold to standard 44.1kHz
# MPEG-1 Layer III, which every browser/OS supports natively. Operates
# directly on R2 storage rather than through the Story/Audio models — the
# same bucket is shared between local/dev and production, but the production
# database isn't reachable from here, so this only needs the R2 credentials
# already in settings.
MIN_SAFE_SAMPLE_RATE = 32000
TARGET_SAMPLE_RATE = 44100


class Command(BaseCommand):
    help = (
        "Re-encodes audio files in R2 storage that use a low/legacy MP3 "
        "sample rate to a standard, universally browser-compatible encoding."
    )

    def add_arguments(self, parser):
        parser.add_argument("--prefix", default="story_audios/", help="R2 key prefix to scan.")
        parser.add_argument(
            "--force", action="store_true", help="Re-encode every file regardless of detected sample rate."
        )
        parser.add_argument("--dry-run", action="store_true", help="Only report which files would be re-encoded.")
        parser.add_argument(
            "--backup-dir",
            default=None,
            help="If set, saves a copy of each original file here before overwriting it in R2.",
        )

    def handle(self, *args, **options):
        client = boto3.client(
            "s3",
            endpoint_url=settings.AWS_S3_ENDPOINT_URL,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=getattr(settings, "AWS_S3_REGION_NAME", "auto"),
        )
        bucket = settings.AWS_STORAGE_BUCKET_NAME
        prefix = options["prefix"]

        paginator = client.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].lower().endswith(".mp3"):
                    keys.append(obj["Key"])

        self.stdout.write(f"Found {len(keys)} mp3 file(s) under '{prefix}' in bucket '{bucket}'.")

        fixed, skipped, failed = 0, 0, 0
        for key in keys:
            with tempfile.NamedTemporaryFile(suffix=".mp3") as src_file:
                client.download_fileobj(bucket, key, src_file)
                src_file.flush()

                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=sample_rate",
                        "-of", "default=noprint_wrappers=1:nokey=1", src_file.name,
                    ],
                    capture_output=True,
                    text=True,
                )
                try:
                    sample_rate = int(probe.stdout.strip())
                except ValueError:
                    self.stdout.write(self.style.WARNING(f"  [skip] {key}: could not probe sample rate"))
                    failed += 1
                    continue

                needs_fix = options["force"] or sample_rate < MIN_SAFE_SAMPLE_RATE
                if not needs_fix:
                    skipped += 1
                    continue

                action = "would fix" if options["dry_run"] else "fixing"
                self.stdout.write(f"  [{action}] {key} (sample_rate={sample_rate})")
                if options["dry_run"]:
                    fixed += 1
                    continue

                if options["backup_dir"]:
                    os.makedirs(options["backup_dir"], exist_ok=True)
                    backup_path = os.path.join(options["backup_dir"], key.replace("/", "__"))
                    src_file.seek(0)
                    with open(backup_path, "wb") as backup_file:
                        shutil.copyfileobj(src_file, backup_file)

                with tempfile.NamedTemporaryFile(suffix=".mp3") as dst_file:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-y", "-i", src_file.name,
                            "-ar", str(TARGET_SAMPLE_RATE), "-b:a", "64k", "-codec:a", "libmp3lame",
                            dst_file.name,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        self.stdout.write(self.style.ERROR(f"    ffmpeg failed: {result.stderr[-300:]}"))
                        failed += 1
                        continue

                    dst_file.seek(0)
                    client.upload_fileobj(dst_file, bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
                    fixed += 1

        self.stdout.write(self.style.SUCCESS(f"Done. Fixed {fixed}, skipped {skipped}, failed {failed}."))

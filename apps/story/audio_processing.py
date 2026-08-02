"""Normalizes uploaded audio to a browser-universally-decodable encoding.

Some sources (older LibriVox "64kbps" releases in particular) publish MP3s
encoded as MPEG-2 Layer III at 22.05kHz — a legacy variant that Android's
media stack tolerates but desktop browsers and iOS Safari's decoders reject
outright. This re-encodes anything below a safe sample rate to standard
44.1kHz MPEG-1 Layer III at upload time, so the problem can't recur one
upload at a time — see management command fix_audio_encoding for the
one-off batch fix this same logic was extracted from.

Needs the ffmpeg/ffprobe system binaries (not a pip package — quality MP3
re-encoding needs a real encoder). If they aren't installed, normalization
is skipped and the upload proceeds with the original file unchanged; run
`manage.py fix_audio_encoding` manually as a fallback in that case.
"""
import logging
import shutil
import subprocess
import tempfile

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

MIN_SAFE_SAMPLE_RATE = 32000
TARGET_SAMPLE_RATE = 44100


def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def normalize_uploaded_audio(uploaded_file):
    """Given an in-request UploadedFile for a new/replacement audio_file
    (not yet saved anywhere), returns a file to actually save — either the
    original (rewound), or a re-encoded in-memory replacement if the source
    uses a legacy sample rate. Runs before the first save, so there's no
    need to deal with storage overwrite/renaming semantics."""
    if not _ffmpeg_available():
        logger.warning("ffmpeg/ffprobe not available — skipping audio normalization for %s", uploaded_file.name)
        return uploaded_file

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as src_tmp:
            for chunk in uploaded_file.chunks():
                src_tmp.write(chunk)
            src_tmp.flush()

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=sample_rate",
                    "-of", "default=noprint_wrappers=1:nokey=1", src_tmp.name,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            sample_rate = int(probe.stdout.strip())

            uploaded_file.seek(0)
            if sample_rate >= MIN_SAFE_SAMPLE_RATE:
                return uploaded_file

            with tempfile.NamedTemporaryFile(suffix=".mp3") as dst_tmp:
                result = subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", src_tmp.name,
                        "-ar", str(TARGET_SAMPLE_RATE), "-b:a", "64k", "-codec:a", "libmp3lame",
                        dst_tmp.name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    logger.error("ffmpeg normalization failed for %s: %s", uploaded_file.name, result.stderr[-500:])
                    return uploaded_file

                dst_tmp.seek(0)
                content = dst_tmp.read()
                logger.info(
                    "Normalized %s from %dHz to %dHz on upload", uploaded_file.name, sample_rate, TARGET_SAMPLE_RATE
                )
                return ContentFile(content, name=uploaded_file.name)
    except (ValueError, subprocess.SubprocessError, OSError):
        logger.exception("Audio normalization failed for %s", uploaded_file.name)
        uploaded_file.seek(0)
        return uploaded_file

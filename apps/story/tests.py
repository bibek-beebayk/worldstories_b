import csv
import io
import os
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from ebooklib import epub
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from storages.backends.s3 import S3Storage

import anthropic
import openpyxl
from pydantic import ValidationError as PydanticValidationError

from apps.stats.models import AnalyticsEvent, StoryCompletion, VideoWatchProgress
from apps.story.api import StoryMapAPIView, StoryViewSet, open_s3_audio_stream
from apps.story.models import HeroTemplate, Mood, StoryJourney, StoryMood, StoryReaction
from apps.story.ai_generation import GenerationError, _GenerationOutput, _to_plain_text, generate
from apps.story.ai_generation_jobs import _concatenated_chapter_text, run_generate_blog_excerpt, run_generate_field
from apps.story.book_fetch import (
    DEFAULT_BOOK_FETCH_COUNT,
    MAX_BOOK_FETCH_COUNT,
    BookFetchError,
    _BookFetchOutput,
    _BookRecord,
    _max_tokens_for,
    fetch_books,
)
from apps.story.book_fetch_jobs import run_book_fetch
from apps.story.queue_records import resolve_story_type
from apps.story.queue_import import (
    MAX_IMPORT_ROWS,
    ImportFileError,
    build_preview,
    confirm_import,
    parse_uploaded_file,
)
from apps.story.taxonomy_bulk_update import (
    build_taxonomy_preview,
    confirm_taxonomy_update,
    resolve_story_match,
)
from apps.story.epub_import import EpubParseError, extract_chapters
from apps.story.excerpts import _excerpt_from_text
from apps.story.epub_import_jobs import run_epub_import
from apps.story.models import (
    Audio,
    AudioTranscriptCue,
    Video,
    VideoSeries,
    VideoType,
    Author,
    Blog,
    BlogTopic,
    BookFetchJob,
    Category,
    Chapter,
    EpubImportJob,
    Favorite,
    Genre,
    PromptSettings,
    Review,
    Story,
    StoryJourneyItem,
    DailyStory,
    StoryQueue,
    StoryType,
    Tag,
    Theme,
)
from apps.story.serializers import (
    AudioAdminSerializer,
    AudioListSerializer,
    AudioSerializer,
    AudioTranscriptCueSerializer,
    StoryAdminSerializer,
    SubmissionSerializer,
    validate_cue_sequence,
)
from apps.story import reading_time
from apps.story.transcripts import (
    TranscriptParseError,
    format_from_filename,
    parse_transcript,
)
from apps.users.models import User
from core.urls import sitemap


class StoryNumericSlugGuardTests(TestCase):
    def test_new_all_digit_slug_gets_story_suffix(self):
        story = Story.objects.create(title="1984", slug="1984", is_published=False)

        self.assertEqual(story.slug, "1984-story")

    def test_existing_numeric_slug_is_not_changed_on_update(self):
        story = Story(title="Legacy", slug="123", is_published=False)
        Story.objects.bulk_create([story])
        story = Story.objects.get(title="Legacy")

        story.title = "Legacy updated"
        story.save(update_fields=["title"])

        self.assertEqual(story.slug, "123")


def _build_test_epub(guide=None, toc=None, spine_items=None, extra_items=None):
    """Builds a small real in-memory EPUB (via ebooklib) for exercising
    extract_chapters against actual EPUB container/spine/TOC structure,
    rather than hand-rolled fixture HTML."""
    book = epub.EpubBook()
    book.set_identifier("test-id")
    book.set_title("Test Book")
    book.set_language("en")

    nav = epub.EpubNav()
    book.add_item(nav)
    book.add_item(epub.EpubNcx())

    items = spine_items or []
    for item in items:
        book.add_item(item)
    for item in extra_items or []:
        book.add_item(item)

    if guide:
        book.guide.extend(guide)
    book.toc = toc or ()
    book.spine = ["nav"] + items

    buf = BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()


class AudioAdminSerializerDurationProbeTests(SimpleTestCase):
    """Regression coverage for a production incident: probing duration from
    instance.audio_file *after* it was already saved to S3/R2 meant a live
    network round-trip back to remote storage on every admin audio upload. A
    stalled connection there hung the request — and with gunicorn's default
    single sync worker, the whole site — until the worker-timeout killed it.
    Duration must be probed from the local pre-save upload instead."""

    def test_probe_duration_from_upload_reads_local_bytes_and_rewinds(self):
        serializer = AudioAdminSerializer()
        uploaded = SimpleUploadedFile("clip.mp3", b"fake-mp3-bytes", content_type="audio/mpeg")

        with patch(
            "apps.story.serializers.reading_time.probe_audio_duration_from_bytes",
            return_value=12.5,
        ) as probe_bytes:
            duration = serializer._probe_duration_from_upload(uploaded)

        probe_bytes.assert_called_once_with(b"fake-mp3-bytes")
        self.assertEqual(duration, 12.5)
        # Rewound so the real save immediately after (which reads the file
        # again to upload it) still gets the full content.
        self.assertEqual(uploaded.tell(), 0)

    def test_create_probes_the_local_upload_not_remote_storage(self):
        serializer = AudioAdminSerializer()
        uploaded = SimpleUploadedFile("clip.mp3", b"fake-mp3-bytes", content_type="audio/mpeg")
        created_instance = SimpleNamespace(duration_seconds=None, save=MagicMock())

        with patch(
            "rest_framework.serializers.ModelSerializer.create",
            return_value=created_instance,
        ), patch(
            "apps.story.serializers.reading_time.probe_audio_duration_from_bytes",
            return_value=7.0,
        ) as probe_bytes, patch(
            "apps.story.serializers.reading_time.probe_audio_duration_seconds"
        ) as probe_remote:
            result = serializer.create({"audio_file": uploaded})

        probe_bytes.assert_called_once()
        probe_remote.assert_not_called()
        self.assertEqual(result.duration_seconds, 7.0)
        created_instance.save.assert_called_once_with(update_fields=["duration_seconds"])

    def test_update_without_a_new_file_does_not_probe(self):
        serializer = AudioAdminSerializer()
        existing_instance = SimpleNamespace(duration_seconds=42.0, save=MagicMock())

        with patch(
            "rest_framework.serializers.ModelSerializer.update",
            return_value=existing_instance,
        ), patch(
            "apps.story.serializers.reading_time.probe_audio_duration_from_bytes"
        ) as probe_bytes:
            result = serializer.update(existing_instance, {"title": "Renamed"})

        probe_bytes.assert_not_called()
        existing_instance.save.assert_not_called()
        self.assertEqual(result.duration_seconds, 42.0)


class AudioTranscriptSerializerTests(TestCase):
    def setUp(self):
        self.story = Story.objects.create(title="Transcript Story", slug="transcript-story")

    def test_create_persists_rich_text_transcript(self):
        serializer = AudioAdminSerializer()
        transcript = "<h2>Opening</h2><p>A <strong>formatted</strong> transcript.</p>"

        with patch.object(serializer, "_probe_duration_from_upload", return_value=None):
            audio = serializer.create(
                {
                    "story": self.story,
                    "title": "Chapter One Audio",
                    "slug": "chapter-one-audio",
                    "audio_file": "story_audios/chapter-one.mp3",
                    "order": 1,
                    "transcript": transcript,
                }
            )

        audio.refresh_from_db()
        self.assertEqual(audio.transcript, transcript)

    def test_existing_audio_defaults_to_blank_transcript_without_changing_file(self):
        audio = Audio.objects.create(
            story=self.story,
            title="Existing Audio",
            slug="existing-audio",
            audio_file="story_audios/existing.mp3",
            order=1,
        )

        audio.refresh_from_db()
        self.assertEqual(audio.transcript, "")
        self.assertEqual(audio.audio_file.name, "story_audios/existing.mp3")

    def test_update_and_explicit_clear_are_persisted(self):
        audio = Audio.objects.create(
            story=self.story,
            title="Editable Audio",
            slug="editable-audio",
            audio_file="story_audios/editable.mp3",
            transcript="<p>Original</p>",
            order=1,
        )

        update = AudioAdminSerializer(audio, data={"transcript": "<p>Updated</p>"}, partial=True)
        self.assertTrue(update.is_valid(), update.errors)
        update.save()
        audio.refresh_from_db()
        self.assertEqual(audio.transcript, "<p>Updated</p>")

        clear = AudioAdminSerializer(audio, data={"transcript": ""}, partial=True)
        self.assertTrue(clear.is_valid(), clear.errors)
        clear.save()
        audio.refresh_from_db()
        self.assertEqual(audio.transcript, "")

    def test_chapter_html_is_an_independent_snapshot_after_copy_result_is_saved(self):
        chapter = Chapter.objects.create(
            story=self.story,
            title="Source Chapter",
            slug="source-chapter",
            content="<p>Original chapter text.</p>",
            order=1,
        )
        audio = Audio.objects.create(
            story=self.story,
            title="Copied Audio",
            slug="copied-audio",
            audio_file="story_audios/copied.mp3",
            order=1,
        )

        serializer = AudioAdminSerializer(audio, data={"transcript": chapter.content}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        chapter.content = "<p>Chapter changed later.</p>"
        chapter.save(update_fields=["content"])

        audio.refresh_from_db()
        self.assertEqual(audio.transcript, "<p>Original chapter text.</p>")


class ReadAlongAPITests(APITestCase):
    def setUp(self):
        self.story = Story.objects.create(title="Read Along Story", slug="read-along-story")
        self.first = Audio.objects.create(
            story=self.story,
            title="First Track",
            slug="first-track",
            audio_file="story_audios/first.mp3",
            transcript="<p>First transcript.</p>",
            order=1,
            duration_seconds=10,
            file_size_bytes=100,
        )
        self.current = Audio.objects.create(
            story=self.story,
            title="Current Track",
            slug="current-track",
            audio_file="story_audios/current.mp3",
            transcript='<script>alert("unsafe")</script><h2>Current</h2><p>Safe text.</p>',
            order=2,
            duration_seconds=20,
            file_size_bytes=200,
        )
        self.without_transcript = Audio.objects.create(
            story=self.story,
            title="Audio Only",
            slug="audio-only",
            audio_file="story_audios/audio-only.mp3",
            transcript="<p><br></p>",
            order=3,
        )
        self.last = Audio.objects.create(
            story=self.story,
            title="Last Track",
            slug="last-track",
            audio_file="story_audios/last.mp3",
            transcript="<p>Last transcript.</p>",
            order=4,
        )

    def read_along_url(self, story_slug=None, audio_slug=None):
        return reverse(
            "story-read-along",
            kwargs={
                "slug": story_slug or self.story.slug,
                "audio_slug": audio_slug or self.current.slug,
            },
        )

    def test_success_returns_metadata_sanitized_transcript_and_compatible_navigation(self):
        response = self.client.get(self.read_along_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["story"]["slug"], self.story.slug)
        self.assertEqual(response.data["audio"]["slug"], self.current.slug)
        self.assertTrue(response.data["audio"]["read_along_available"])
        self.assertFalse(response.data["audio"]["transcript_synchronized"])
        self.assertEqual(response.data["transcript"]["state"], "unsynchronized")
        self.assertEqual(response.data["transcript"]["cues"], [])
        self.assertIn("<h2>Current</h2>", response.data["transcript"]["html"])
        self.assertNotIn("<script", response.data["transcript"]["html"])
        self.assertNotIn("alert", response.data["transcript"]["html"])
        self.assertTrue(response.data["audio"]["stream_url"].endswith(
            "/api/stories/read-along-story/audios/current-track/stream/"
        ))
        self.assertEqual(
            response.data["navigation"],
            {"previous_audio_slug": "first-track", "next_audio_slug": "last-track"},
        )

    def test_audio_serializers_expose_flags_without_transcript_body(self):
        detailed = AudioSerializer(self.current).data
        listed = AudioListSerializer(self.current).data

        for payload in (detailed, listed):
            self.assertTrue(payload["has_transcript"])
            self.assertTrue(payload["read_along_available"])
            self.assertFalse(payload["transcript_synchronized"])
            self.assertNotIn("transcript", payload)

        unavailable = AudioSerializer(self.without_transcript).data
        self.assertFalse(unavailable["has_transcript"])
        self.assertFalse(unavailable["read_along_available"])

    def test_unpublished_story_is_not_available(self):
        self.story.is_published = False
        self.story.save(update_fields=["is_published"])

        response = self.client.get(self.read_along_url())

        self.assertEqual(response.status_code, 404)

    def test_future_scheduled_story_is_not_available(self):
        self.story.publish_at = timezone.now() + timedelta(days=1)
        self.story.save(update_fields=["publish_at"])

        response = self.client.get(self.read_along_url())

        self.assertEqual(response.status_code, 404)

    def test_audio_from_another_story_is_rejected(self):
        other_story = Story.objects.create(title="Other Story", slug="other-story")
        other_audio = Audio.objects.create(
            story=other_story,
            title="Other Audio",
            slug="other-audio",
            audio_file="story_audios/other.mp3",
            transcript="<p>Other transcript.</p>",
            order=1,
        )

        response = self.client.get(self.read_along_url(audio_slug=other_audio.slug))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["detail"], "Audio track not found.")

    def test_blank_transcript_returns_clear_unavailable_response(self):
        response = self.client.get(self.read_along_url(audio_slug=self.without_transcript.slug))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["transcript"]["state"], "empty")
        self.assertEqual(response.data["transcript"]["html"], "")
        self.assertFalse(response.data["audio"]["read_along_available"])
        self.assertEqual(
            response.data["navigation"],
            {"previous_audio_slug": None, "next_audio_slug": None},
        )

    def test_missing_audio_file_returns_readable_transcript_with_audio_unavailable(self):
        text_only = Audio.objects.create(
            story=self.story,
            title="Transcript Only",
            slug="transcript-only",
            audio_file="",
            transcript="<p>Readable without audio.</p>",
            order=5,
        )

        response = self.client.get(self.read_along_url(audio_slug=text_only.slug))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["audio"]["stream_url"])
        self.assertIsNone(response.data["audio"]["audio_file"])
        self.assertFalse(response.data["audio"]["read_along_available"])
        self.assertEqual(response.data["transcript"]["state"], "unsynchronized")

    def test_success_query_count_is_constant(self):
        # Four SELECTs (story, requested audio, its prefetched transcript cues,
        # navigation) plus the SAVEPOINT/RELEASE pair added by this project's
        # ATOMIC_REQUESTS.
        with self.assertNumQueries(6):
            response = self.client.get(self.read_along_url())

        self.assertEqual(response.status_code, 200)

    def test_default_offset_seconds_reflects_audio_read_along_offset(self):
        response = self.client.get(self.read_along_url())
        self.assertEqual(response.data["transcript"]["default_offset_seconds"], 0)

        self.current.read_along_offset_ms = 500
        self.current.save(update_fields=["read_along_offset_ms"])
        self.current.refresh_from_db()

        response = self.client.get(self.read_along_url())
        self.assertEqual(response.data["transcript"]["default_offset_seconds"], 0.5)

        self.current.read_along_offset_ms = -300
        self.current.save(update_fields=["read_along_offset_ms"])
        response = self.client.get(self.read_along_url())
        self.assertEqual(response.data["transcript"]["default_offset_seconds"], -0.3)

    def test_timed_cues_produce_a_synchronized_response(self):
        AudioTranscriptCue.objects.bulk_create(
            [
                AudioTranscriptCue(audio=self.current, order=1, start_ms=0, end_ms=1500, text="Line one."),
                AudioTranscriptCue(audio=self.current, order=2, start_ms=1500, end_ms=3200, text="Line two."),
                AudioTranscriptCue(audio=self.current, order=3, start_ms=3200, end_ms=4000, text="Line three."),
            ]
        )

        response = self.client.get(self.read_along_url())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["audio"]["transcript_synchronized"])
        self.assertEqual(response.data["transcript"]["state"], "synchronized")
        self.assertTrue(response.data["transcript"]["synchronized"])
        cues = response.data["transcript"]["cues"]
        self.assertEqual([cue["text"] for cue in cues], ["Line one.", "Line two.", "Line three."])
        self.assertEqual(cues[1]["start_seconds"], 1.5)
        self.assertEqual(cues[1]["end_seconds"], 3.2)
        self.assertIn("id", cues[0])

    def test_large_cue_set_serializes_in_stable_order(self):
        AudioTranscriptCue.objects.bulk_create(
            AudioTranscriptCue(
                audio=self.first, order=index, start_ms=index * 1000, end_ms=index * 1000 + 800,
                text=f"Cue {index}",
            )
            for index in range(1, 401)
        )

        response = self.client.get(self.read_along_url(audio_slug=self.first.slug))

        orders_from_text = [int(cue["text"].split()[1]) for cue in response.data["transcript"]["cues"]]
        self.assertEqual(orders_from_text, list(range(1, 401)))

    def test_story_detail_reflects_transcript_synchronized_without_n_plus_one(self):
        AudioTranscriptCue.objects.create(
            audio=self.current, order=1, start_ms=0, end_ms=1000, text="Timed."
        )
        url = reverse("story-detail", kwargs={"slug": self.story.slug})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        flags = {audio["slug"]: audio["transcript_synchronized"] for audio in response.data["audios"]}
        self.assertTrue(flags["current-track"])
        self.assertFalse(flags["first-track"])


class AudioTranscriptCueModelTests(TestCase):
    def setUp(self):
        self.story = Story.objects.create(title="Cue Story", slug="cue-story")
        self.audio = Audio.objects.create(
            story=self.story, title="Track", slug="track",
            audio_file="story_audios/track.mp3", transcript="<p>Transcript.</p>", order=1,
        )

    def test_unique_audio_order(self):
        AudioTranscriptCue.objects.create(audio=self.audio, order=1, start_ms=0, end_ms=1000, text="A")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AudioTranscriptCue.objects.create(
                    audio=self.audio, order=1, start_ms=1000, end_ms=2000, text="B"
                )

    def test_check_constraint_rejects_non_positive_span(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AudioTranscriptCue.objects.create(
                    audio=self.audio, order=1, start_ms=2000, end_ms=2000, text="Zero span"
                )

    def test_clean_rejects_bad_span_and_empty_text(self):
        with self.assertRaises(DjangoValidationError):
            AudioTranscriptCue(audio=self.audio, order=1, start_ms=1000, end_ms=500, text="x").full_clean()
        with self.assertRaises(DjangoValidationError):
            AudioTranscriptCue(audio=self.audio, order=1, start_ms=0, end_ms=1000, text="   ").full_clean()

    def test_deleting_audio_removes_its_cues_and_leaves_transcript_alone(self):
        AudioTranscriptCue.objects.create(audio=self.audio, order=1, start_ms=0, end_ms=1000, text="A")
        other = Audio.objects.create(
            story=self.story, title="Other", slug="other",
            audio_file="story_audios/other.mp3", transcript="<p>Other.</p>", order=2,
        )
        AudioTranscriptCue.objects.create(audio=other, order=1, start_ms=0, end_ms=1000, text="B")

        self.audio.delete()

        self.assertFalse(AudioTranscriptCue.objects.filter(audio_id=self.audio.id).exists())
        self.assertEqual(AudioTranscriptCue.objects.filter(audio=other).count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.transcript, "<p>Other.</p>")

    def test_default_ordering_is_by_order(self):
        for order in (3, 1, 2):
            AudioTranscriptCue.objects.create(
                audio=self.audio, order=order, start_ms=order * 10, end_ms=order * 10 + 5, text=str(order)
            )
        self.assertEqual(
            list(self.audio.transcript_cues.values_list("order", flat=True)), [1, 2, 3]
        )


class AudioTranscriptCueSerializerTests(TestCase):
    def _cue(self, **overrides):
        cue = {"order": 1, "start_ms": 0, "end_ms": 1000, "text": "Line"}
        cue.update(overrides)
        return cue

    def test_single_cue_field_validation(self):
        serializer = AudioTranscriptCueSerializer(data=self._cue(start_ms=1000, end_ms=1000))
        self.assertFalse(serializer.is_valid())
        self.assertIn("end_ms", serializer.errors)

        serializer = AudioTranscriptCueSerializer(data=self._cue(text="  "))
        self.assertFalse(serializer.is_valid())
        self.assertIn("text", serializer.errors)

    def test_sequence_rejects_bad_ordering_and_overlap(self):
        with self.assertRaises(ValidationError):
            validate_cue_sequence([
                self._cue(order=2, start_ms=0, end_ms=1000),
                self._cue(order=1, start_ms=1000, end_ms=2000),
            ])
        with self.assertRaises(ValidationError):
            validate_cue_sequence([
                self._cue(order=1, start_ms=0, end_ms=1500),
                self._cue(order=2, start_ms=1000, end_ms=2000),
            ])

    def test_sequence_rejects_cues_far_beyond_duration(self):
        cues = [self._cue(order=1, start_ms=0, end_ms=60_000)]
        with self.assertRaises(ValidationError):
            validate_cue_sequence(cues, audio_duration_ms=10_000)
        # Small overrun within the grace margin is fine.
        validate_cue_sequence(
            [self._cue(order=1, start_ms=0, end_ms=11_000)], audio_duration_ms=10_000
        )

    def test_valid_sequence_passes_through_list_serializer(self):
        serializer = AudioTranscriptCueSerializer(
            data=[
                self._cue(order=1, start_ms=0, end_ms=1000, text="One"),
                self._cue(order=2, start_ms=1000, end_ms=2000, text="Two"),
            ],
            many=True,
            context={"audio_duration_ms": 3000},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


VALID_VTT = """WEBVTT
Kind: captions
Language: en

NOTE This is a note block
that spans two lines.

STYLE
::cue { color: white }

intro
00:00:00.000 --> 00:00:01.500 align:start position:0%
<v Narrator>Once upon a time &amp; long ago</v>

00:01.500 --> 00:03.200
The second cue
wraps across lines
"""

VALID_SRT = """7
00:00:00,000 --> 00:00:02,000  X1:040 X2:600
<i>Italic</i> {\\an8}opening

7
00:00:02,000 --> 00:00:05,500
Second block
second line
"""


class TranscriptParserTests(SimpleTestCase):
    def test_webvtt_happy_path(self):
        result = parse_transcript(VALID_VTT, "vtt")

        self.assertTrue(result.is_timed)
        self.assertEqual([c.order for c in result.cues], [1, 2])
        first, second = result.cues
        self.assertEqual((first.start_ms, first.end_ms), (0, 1500))
        self.assertEqual(first.text, "Once upon a time & long ago")
        self.assertEqual((second.start_ms, second.end_ms), (1500, 3_200))
        self.assertEqual(second.text, "The second cue wraps across lines")
        self.assertEqual(
            result.transcript_html,
            "<p>Once upon a time &amp; long ago</p><p>The second cue wraps across lines</p>",
        )

    def test_webvtt_tolerates_leading_bom(self):
        result = parse_transcript("﻿" + VALID_VTT, "vtt")
        self.assertEqual(len(result.cues), 2)

    def test_webvtt_missing_header_raises(self):
        with self.assertRaises(TranscriptParseError):
            parse_transcript("00:00:00.000 --> 00:00:01.000\nHi\n", "vtt")

    def test_webvtt_malformed_timestamp_reports_line(self):
        with self.assertRaises(TranscriptParseError) as ctx:
            parse_transcript("WEBVTT\n\n00:00:99.000 --> 00:00:01.000\nHi\n", "vtt")
        self.assertIn("Line 3", str(ctx.exception))

    def test_webvtt_header_only_raises(self):
        with self.assertRaises(TranscriptParseError) as ctx:
            parse_transcript("WEBVTT\n\nNOTE nothing here\n", "vtt")
        self.assertIn("No subtitle cues", str(ctx.exception))

    def test_srt_uses_position_not_index_and_strips_tags(self):
        result = parse_transcript(VALID_SRT, "srt")

        self.assertEqual([c.order for c in result.cues], [1, 2])
        self.assertEqual(result.cues[0].text, "Italic opening")
        self.assertEqual(result.cues[0].end_ms, 2000)
        self.assertEqual(result.cues[1].text, "Second block second line")

    def test_srt_block_without_timing_line_raises(self):
        with self.assertRaises(TranscriptParseError):
            parse_transcript("1\nJust text, no timing\n", "srt")

    def test_end_not_after_start_raises(self):
        with self.assertRaises(TranscriptParseError):
            parse_transcript("WEBVTT\n\n00:00:02.000 --> 00:00:01.000\nHi\n", "vtt")

    def test_plain_text_produces_paragraphs_no_cues(self):
        result = parse_transcript("First paragraph.\n\nSecond <b>paragraph</b> & more.", "text")

        self.assertEqual(result.cues, [])
        self.assertFalse(result.is_timed)
        self.assertEqual(
            result.transcript_html,
            "<p>First paragraph.</p><p>Second &lt;b&gt;paragraph&lt;/b&gt; &amp; more.</p>",
        )

    def test_plain_text_whitespace_only(self):
        self.assertEqual(parse_transcript("   \n\n  ", "text").transcript_html, "")

    def test_format_from_filename(self):
        self.assertEqual(format_from_filename("Clip.VTT"), "vtt")
        self.assertEqual(format_from_filename("clip.srt"), "srt")
        self.assertEqual(format_from_filename("notes.txt"), "text")
        self.assertIsNone(format_from_filename("clip.pdf"))


class AudioTranscriptAdminApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="transcriptadmin@example.com",
            username="transcriptadmin",
            is_superuser=True,
            is_staff=True,
            is_active=True,
        )

    def setUp(self):
        self.client.force_authenticate(user=self._make_superuser())
        self.story = Story.objects.create(title="Import Story", slug="import-story")
        self.audio = Audio.objects.create(
            story=self.story,
            title="Track",
            slug="track",
            audio_file="story_audios/track.mp3",
            transcript="",
            order=1,
            duration_seconds=12.0,
        )
        AudioTranscriptCue.objects.bulk_create(
            [
                AudioTranscriptCue(audio=self.audio, order=1, start_ms=0, end_ms=500, text="old one"),
                AudioTranscriptCue(audio=self.audio, order=2, start_ms=500, end_ms=1000, text="old two"),
            ]
        )

    def _url(self, suffix):
        return f"/api/admin/audios/{self.audio.id}/{suffix}/"

    def test_import_vtt_replaces_cues_and_seeds_empty_transcript(self):
        upload = SimpleUploadedFile("clip.vtt", VALID_VTT.encode("utf-8"), content_type="text/vtt")
        response = self.client.post(self._url("import-transcript"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["transcript_state"], "synchronized")
        self.assertEqual(response.data["cue_count"], 2)
        self.audio.refresh_from_db()
        self.assertEqual(
            list(self.audio.transcript_cues.values_list("text", flat=True)),
            ["Once upon a time & long ago", "The second cue wraps across lines"],
        )
        self.assertEqual(self.audio.transcript, response.data["transcript"])
        self.assertIn("<p>", self.audio.transcript)

    def test_import_via_json_content(self):
        response = self.client.put(
            self._url("transcript-cues"), {"cues": []}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            self._url("import-transcript"),
            {"content": VALID_SRT, "format": "srt"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cue_count"], 2)

    def test_import_leaves_existing_transcript_when_present(self):
        self.audio.transcript = "<p>Hand written transcript.</p>"
        self.audio.save(update_fields=["transcript"])

        upload = SimpleUploadedFile("clip.vtt", VALID_VTT.encode("utf-8"), content_type="text/vtt")
        response = self.client.post(self._url("import-transcript"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.transcript, "<p>Hand written transcript.</p>")
        self.assertEqual(self.audio.transcript_cues.count(), 2)

    def test_import_plain_text_overwrites_transcript_with_no_cues(self):
        self.audio.transcript = "<p>Old.</p>"
        self.audio.save(update_fields=["transcript"])
        upload = SimpleUploadedFile("t.txt", b"Line one.\n\nLine two.", content_type="text/plain")

        response = self.client.post(self._url("import-transcript"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["transcript_state"], "unsynchronized")
        self.assertEqual(response.data["cue_count"], 0)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.transcript, "<p>Line one.</p><p>Line two.</p>")
        self.assertEqual(self.audio.transcript_cues.count(), 0)

    def test_malformed_import_is_atomic(self):
        upload = SimpleUploadedFile("bad.vtt", b"00:00:00.000 --> 00:00:01.000\nHi", content_type="text/vtt")
        response = self.client.post(self._url("import-transcript"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
        self.assertEqual(self.audio.transcript_cues.count(), 2)

    def test_cues_beyond_duration_are_rejected_without_touching_existing(self):
        far = "WEBVTT\n\n00:00:00.000 --> 00:01:00.000\nToo long\n"
        upload = SimpleUploadedFile("far.vtt", far.encode("utf-8"), content_type="text/vtt")

        response = self.client.post(self._url("import-transcript"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.audio.transcript_cues.count(), 2)

    def test_bad_extension_rejected(self):
        upload = SimpleUploadedFile("clip.pdf", b"whatever", content_type="application/pdf")
        response = self.client.post(self._url("import-transcript"), {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_non_superuser_forbidden(self):
        self.client.force_authenticate(
            user=User.objects.create(email="plain@example.com", username="plain", is_active=True)
        )
        response = self.client.post(
            self._url("import-transcript"), {"content": VALID_SRT, "format": "srt"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_clear_transcript_removes_cues_and_text(self):
        self.audio.transcript = "<p>Something.</p>"
        self.audio.save(update_fields=["transcript"])

        response = self.client.post(self._url("clear-transcript"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["transcript_state"], "empty")
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.transcript, "")
        self.assertEqual(self.audio.transcript_cues.count(), 0)

    def test_put_transcript_cues_replaces(self):
        self.audio.transcript = "<p>A B</p>"
        self.audio.save(update_fields=["transcript"])
        response = self.client.put(
            self._url("transcript-cues"),
            {"cues": [
                {"order": 1, "start_ms": 0, "end_ms": 1000, "text": "A"},
                {"order": 2, "start_ms": 1000, "end_ms": 2000, "text": "B"},
            ]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cue_count"], 2)
        self.assertEqual(response.data["transcript"], "<p>A B</p>")
        self.assertEqual(
            list(self.audio.transcript_cues.values_list("text", flat=True)), ["A", "B"]
        )

    def test_put_transcript_cues_rejects_overlap_and_keeps_existing(self):
        self.audio.transcript = "<p>A B</p>"
        self.audio.save(update_fields=["transcript"])
        response = self.client.put(
            self._url("transcript-cues"),
            {"cues": [
                {"order": 1, "start_ms": 0, "end_ms": 1500, "text": "A"},
                {"order": 2, "start_ms": 1000, "end_ms": 2000, "text": "B"},
            ]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.audio.transcript_cues.count(), 2)

    def test_put_timed_cues_requires_transcript_text(self):
        response = self.client.put(
            self._url("transcript-cues"),
            {"cues": [{"order": 1, "start_ms": 0, "end_ms": 1000, "text": "A"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("transcript text", response.data["detail"])
        self.assertEqual(self.audio.transcript_cues.count(), 2)

    def test_get_transcript_cues(self):
        response = self.client.get(self._url("transcript-cues"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cue_count"], 2)
        self.assertEqual([c["order"] for c in response.data["cues"]], [1, 2])

    def test_admin_list_exposes_transcript_state_per_row(self):
        self.audio.transcript = "<p>Timed text.</p>"
        self.audio.save(update_fields=["transcript"])
        Audio.objects.create(
            story=self.story, title="Other", slug="other",
            audio_file="story_audios/other.mp3", transcript="<p>Text.</p>", order=2,
        )
        Audio.objects.create(
            story=self.story, title="Third", slug="third",
            audio_file="story_audios/third.mp3", transcript="", order=3,
        )

        # Two data queries (count + page) regardless of row count — the flags
        # come from an annotation on the queryset, not a per-row lookup — plus
        # the ATOMIC_REQUESTS savepoint/release pair.
        with self.assertNumQueries(4):
            response = self.client.get(f"/api/admin/audios/?story={self.story.id}")

        rows = {row["slug"]: row for row in response.data["results"]}
        self.assertTrue(rows["track"]["transcript_synchronized"])
        self.assertEqual(rows["track"]["cue_count"], 2)
        self.assertFalse(rows["other"]["transcript_synchronized"])
        self.assertEqual(rows["other"]["cue_count"], 0)

    def test_superuser_patches_read_along_offset(self):
        response = self.client.patch(
            f"/api/admin/audios/{self.audio.id}/",
            {"read_along_offset_ms": 750},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.read_along_offset_ms, 750)

    def test_read_along_offset_out_of_range_is_rejected(self):
        response = self.client.patch(
            f"/api/admin/audios/{self.audio.id}/",
            {"read_along_offset_ms": 9000},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.read_along_offset_ms, 0)

    def test_non_superuser_cannot_patch_read_along_offset(self):
        self.client.force_authenticate(user=None)
        response = self.client.patch(
            f"/api/admin/audios/{self.audio.id}/",
            {"read_along_offset_ms": 250},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))

        reader = User.objects.create(
            email="reader@example.com", username="reader", is_active=True
        )
        self.client.force_authenticate(user=reader)
        response = self.client.patch(
            f"/api/admin/audios/{self.audio.id}/",
            {"read_along_offset_ms": 250},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class StoryAdminSerializerFileReadingCacheTests(SimpleTestCase):
    """Same production-incident class as AudioAdminSerializerDurationProbeTests
    above, but for epub/pdf reading-time estimates: these used to be parsed
    live from remote storage on every chapterless-story detail-page view
    (a much hotter path than admin audio uploads). They're now cached on
    Story.cached_file_reading_minutes, computed from the local upload at
    admin save time — never fetched from remote storage in a live request."""

    def _create_with(self, epub_file=None, pdf_file=None):
        serializer = StoryAdminSerializer()
        created_instance = SimpleNamespace(cached_file_reading_minutes=None)
        validated_data = {}
        if epub_file is not None:
            validated_data["epub_file"] = epub_file
        if pdf_file is not None:
            validated_data["pdf_file"] = pdf_file

        with patch(
            "rest_framework.serializers.ModelSerializer.create",
            side_effect=lambda data: created_instance,
        ) as create_super:
            serializer.create(validated_data)
        return create_super.call_args.args[0]

    def test_create_with_epub_upload_caches_from_local_bytes(self):
        uploaded = SimpleUploadedFile("book.epub", b"epub-bytes", content_type="application/epub+zip")
        with patch(
            "apps.story.serializers.reading_time.epub_minutes_from_bytes", return_value=15
        ) as epub_probe, patch(
            "apps.story.serializers.reading_time.pdf_minutes_from_bytes"
        ) as pdf_probe:
            submitted = self._create_with(epub_file=uploaded)

        epub_probe.assert_called_once_with(b"epub-bytes")
        pdf_probe.assert_not_called()
        self.assertEqual(submitted["cached_file_reading_minutes"], 15)

    def test_create_with_pdf_only_upload_caches_from_local_bytes(self):
        uploaded = SimpleUploadedFile("book.pdf", b"pdf-bytes", content_type="application/pdf")
        with patch(
            "apps.story.serializers.reading_time.pdf_minutes_from_bytes", return_value=8
        ) as pdf_probe:
            submitted = self._create_with(pdf_file=uploaded)

        pdf_probe.assert_called_once_with(b"pdf-bytes")
        self.assertEqual(submitted["cached_file_reading_minutes"], 8)

    def test_create_with_both_files_prefers_epub(self):
        epub_uploaded = SimpleUploadedFile("book.epub", b"epub-bytes", content_type="application/epub+zip")
        pdf_uploaded = SimpleUploadedFile("book.pdf", b"pdf-bytes", content_type="application/pdf")
        with patch(
            "apps.story.serializers.reading_time.epub_minutes_from_bytes", return_value=15
        ), patch(
            "apps.story.serializers.reading_time.pdf_minutes_from_bytes"
        ) as pdf_probe:
            submitted = self._create_with(epub_file=epub_uploaded, pdf_file=pdf_uploaded)

        pdf_probe.assert_not_called()
        self.assertEqual(submitted["cached_file_reading_minutes"], 15)

    def _update(self, instance, validated_data):
        serializer = StoryAdminSerializer()
        with patch(
            "rest_framework.serializers.ModelSerializer.update",
            side_effect=lambda inst, data: inst,
        ) as update_super:
            serializer.update(instance, validated_data)
        return update_super.call_args.args[1]

    def test_update_removing_the_only_file_clears_the_cache(self):
        instance = SimpleNamespace(
            epub_file=MagicMock(__bool__=lambda self: True, delete=MagicMock()),
            pdf_file=None,
            cover_image_file=None,
            cached_file_reading_minutes=15,
        )
        submitted = self._update(instance, {"remove_epub_file": True})
        self.assertIsNone(submitted["cached_file_reading_minutes"])

    def test_update_removing_one_file_leaves_the_surviving_files_cache_untouched(self):
        # epub is being removed but a pdf survives, unchanged, un-reuploaded —
        # recomputing from it would mean fetching it back from remote storage,
        # so the existing cached estimate (whatever it is) is left alone.
        instance = SimpleNamespace(
            epub_file=MagicMock(__bool__=lambda self: True, delete=MagicMock()),
            pdf_file=MagicMock(__bool__=lambda self: True),
            cover_image_file=None,
            cached_file_reading_minutes=15,
        )
        submitted = self._update(instance, {"remove_epub_file": True})
        self.assertNotIn("cached_file_reading_minutes", submitted)

    def test_update_uploading_new_epub_recomputes_even_if_a_pdf_already_exists(self):
        instance = SimpleNamespace(
            epub_file=None,
            pdf_file=MagicMock(__bool__=lambda self: True),
            cover_image_file=None,
            cached_file_reading_minutes=8,
        )
        uploaded = SimpleUploadedFile("book.epub", b"epub-bytes", content_type="application/epub+zip")
        with patch(
            "apps.story.serializers.reading_time.epub_minutes_from_bytes", return_value=20
        ) as epub_probe:
            submitted = self._update(instance, {"epub_file": uploaded})

        epub_probe.assert_called_once_with(b"epub-bytes")
        self.assertEqual(submitted["cached_file_reading_minutes"], 20)


class StoryReadingMinutesTests(SimpleTestCase):
    def test_chapterless_story_returns_the_cached_value_without_touching_storage(self):
        story = SimpleNamespace(
            chapters=SimpleNamespace(exists=lambda: False),
            cached_file_reading_minutes=12,
        )
        with patch("apps.story.reading_time.epub_reading_minutes") as epub_probe, patch(
            "apps.story.reading_time.pdf_reading_minutes"
        ) as pdf_probe:
            result = reading_time.story_reading_minutes(story)

        epub_probe.assert_not_called()
        pdf_probe.assert_not_called()
        self.assertEqual(result, 12)

    def test_story_with_chapters_ignores_the_cached_file_value(self):
        story = SimpleNamespace(
            chapters=SimpleNamespace(
                exists=lambda: True,
                values_list=lambda *a, **k: ["<p>" + " ".join(["word"] * 400) + "</p>"],
            ),
            cached_file_reading_minutes=999,
        )
        result = reading_time.story_reading_minutes(story)
        self.assertEqual(result, 2)


class SummaryReadingMinutesTests(SimpleTestCase):
    def test_empty_summary_returns_none(self):
        self.assertIsNone(reading_time.summary_reading_minutes(""))
        self.assertIsNone(reading_time.summary_reading_minutes(None))

    def test_uses_220_words_per_minute_not_the_general_200_rate(self):
        # 300 words is a count where the two rates actually disagree
        # (300/220 -> 1 min, 300/200 -> 2 min) — using the wrong constant
        # here would silently pass a test built on a count where they
        # happen to agree, so this is the one that actually catches it.
        html = "<p>" + " ".join(["word"] * 300) + "</p>"
        self.assertEqual(reading_time.summary_reading_minutes(html), 1)
        self.assertEqual(reading_time._minutes_from_word_count(300), 2)


class HomeDataQuickReadsTests(APITestCase):
    def setUp(self):
        # /api/home/ is cache_page-wrapped; LocMemCache persists across tests
        # in-process, so clear it or a later test sees an earlier one's payload.
        cache.clear()

    def _make_home_stories(self, count, *, prefix):
        genre = Genre.objects.create(name=f"Genre {prefix}")
        reviewer = User.objects.create(
            email=f"{prefix}-reviewer@example.com", username=f"{prefix}-reviewer", is_active=True
        )
        for i in range(count):
            story = Story.objects.create(
                title=f"{prefix} Story {i}",
                slug=f"{prefix}-story-{i}",
                is_published=True,
                views=i,
                rating=(i % 5),
            )
            story.genres.add(genre)
            Audio.objects.create(
                story=story, title="Track", slug=f"{prefix}-audio-{i}",
                audio_file="story_audios/x.mp3", order=1,
            )
            Review.objects.create(story=story, user=reviewer, rating=4)
            Favorite.objects.create(story=story, user=reviewer)

    def test_home_data_query_count_does_not_grow_with_the_catalogue(self):
        # The hero + rails serialize dozens of story cards; a per-row
        # count()/exists() there is what made /api/home/ crawl on a cold DB.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._make_home_stories(8, prefix="small")
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get(reverse("home-data")).status_code, 200)

        cache.clear()
        self._make_home_stories(12, prefix="big")
        with CaptureQueriesContext(connection) as big:
            self.assertEqual(self.client.get(reverse("home-data")).status_code, 200)

        # Same query count for a much larger catalogue → no per-row N+1.
        self.assertEqual(len(big.captured_queries), len(small.captured_queries))

    def test_quick_reads_only_includes_published_stories_with_a_summary(self):
        Story.objects.create(
            title="Has Summary",
            slug="has-summary",
            summary="<p>" + " ".join(["word"] * 50) + "</p>",
            is_published=True,
        )
        Story.objects.create(
            title="No Summary",
            slug="no-summary",
            summary="",
            is_published=True,
        )
        Story.objects.create(
            title="Draft With Summary",
            slug="draft-with-summary",
            summary="<p>word word word</p>",
            is_published=False,
        )

        response = self.client.get(reverse("home-data"))

        self.assertEqual(response.status_code, 200)
        quick_read_slugs = [story["slug"] for story in response.data["quick_reads"]]
        self.assertEqual(quick_read_slugs, ["has-summary"])
        self.assertIsNotNone(response.data["quick_reads"][0]["summary_reading_minutes"])

    def test_manual_featured_stories_replace_the_hero_with_no_backfill(self):
        # Manual rank order beats view count — the low-view pick still leads.
        second_pick = Story.objects.create(
            title="Second Pick", slug="second-pick", is_published=True, views=1, featured_rank=2
        )
        first_pick = Story.objects.create(
            title="First Pick", slug="first-pick", is_published=True, views=1000, featured_rank=1
        )
        for i, views in enumerate([500, 400, 300, 200], start=1):
            Story.objects.create(
                title=f"Not Featured {i}", slug=f"not-featured-{i}", is_published=True, views=views
            )

        response = self.client.get(reverse("home-data"))

        self.assertEqual(response.status_code, 200)
        slugs = [story["slug"] for story in response.data["featured_stories"]]
        self.assertEqual(slugs, ["first-pick", "second-pick"])

    def test_no_manual_featured_stories_falls_back_to_fully_automatic(self):
        for i, views in enumerate([500, 400, 300, 200, 100, 50], start=1):
            Story.objects.create(
                title=f"Auto {i}", slug=f"auto-{i}", is_published=True, views=views
            )

        response = self.client.get(reverse("home-data"))

        self.assertEqual(response.status_code, 200)
        slugs = [story["slug"] for story in response.data["featured_stories"]]
        self.assertEqual(slugs, ["auto-1", "auto-2", "auto-3", "auto-4", "auto-5"])

    def test_active_daily_story_replaces_the_hero_for_the_utc_date(self):
        daily = Story.objects.create(
            title="Today's Choice", slug="todays-choice", is_published=True,
            country="NP", summary="<p>A short summary.</p>", cached_chapter_reading_minutes=12,
        )
        DailyStory.objects.create(
            date=timezone.localdate(), story=daily, featured_reason="A journey worth taking."
        )

        response = self.client.get(reverse("home-data"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["daily_story"]["configured"])
        self.assertEqual(response.data["daily_story"]["story"]["slug"], daily.slug)
        self.assertEqual(response.data["daily_story"]["story"]["country"], "NP")
        self.assertEqual(response.data["daily_story"]["featured_reason"], "A journey worth taking.")

    def test_missing_or_inactive_daily_story_falls_back_to_featured_lead(self):
        lead = Story.objects.create(
            title="Hero Lead", slug="hero-lead", is_published=True, featured_rank=1
        )
        inactive = Story.objects.create(title="Inactive", slug="inactive-daily", is_published=True)
        DailyStory.objects.create(date=timezone.localdate(), story=inactive, active=False)

        response = self.client.get(reverse("home-data"))

        self.assertFalse(response.data["daily_story"]["configured"])
        self.assertEqual(response.data["daily_story"]["story"]["slug"], lead.slug)

    def test_originals_section_only_lists_flagged_published_stories(self):
        original = Story.objects.create(
            title="An Original", slug="an-original", is_published=True, is_original=True
        )
        Story.objects.create(
            title="Regular", slug="regular", is_published=True, is_original=False
        )
        Story.objects.create(
            title="Draft Original", slug="draft-original", is_published=False, is_original=True
        )

        response = self.client.get(reverse("home-data"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("originals", response.data)
        self.assertEqual([s["slug"] for s in response.data["originals"]], ["an-original"])
        self.assertTrue(response.data["originals"][0]["is_original"])


class DailyStoryAdminApiTests(APITestCase):
    def setUp(self):
        admin = User.objects.create_user(
            email="daily-admin@example.com", username="daily-admin", password="x",
            is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(admin)
        self.story = Story.objects.create(title="Daily Pick", slug="daily-pick", is_published=True)
        self.url = reverse("admin-story-daily")

    def test_create_update_read_and_delete_a_date(self):
        selected_date = "2026-09-07"
        created = self.client.put(
            self.url,
            {"date": selected_date, "story": self.story.id, "featured_reason": "Read this.", "active": True},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["story_detail"]["slug"], self.story.slug)

        fetched = self.client.get(self.url, {"date": selected_date})
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.data["featured_reason"], "Read this.")

        updated = self.client.put(
            self.url,
            {"date": selected_date, "story": self.story.id, "featured_reason": "Updated.", "active": False},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.data["active"])

        self.assertEqual(self.client.delete(self.url, {"date": selected_date}).status_code, 204)
        self.assertFalse(DailyStory.objects.filter(date=selected_date).exists())

    def test_rejects_an_unpublished_story(self):
        draft = Story.objects.create(title="Draft", slug="daily-draft", is_published=False)
        response = self.client.put(
            self.url,
            {"date": "2026-09-08", "story": draft.id, "featured_reason": "", "active": True},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

class AudioStreamRangeTests(SimpleTestCase):
    def make_view(self, payload=b"0123456789"):
        audio_file = MagicMock()
        audio_file.size = len(payload)
        audio_file.name = "story_audios/chapter.mp3"
        audio_file.open.return_value = BytesIO(payload)
        audio = SimpleNamespace(audio_file=audio_file)
        story = MagicMock()
        story.audios.filter.return_value.first.return_value = audio
        view = StoryViewSet()
        view.get_object = MagicMock(return_value=story)
        return view

    def test_audio_stream_serves_requested_byte_range(self):
        view = self.make_view()
        request = RequestFactory().get("/audio", HTTP_RANGE="bytes=2-5")

        response = view.audio_stream(request, slug="story", audio_slug="chapter")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assertEqual(response["Content-Length"], "4")
        self.assertEqual(b"".join(response.streaming_content), b"2345")

    def test_audio_stream_rejects_unsatisfiable_range(self):
        view = self.make_view()
        request = RequestFactory().get("/audio", HTTP_RANGE="bytes=20-30")

        response = view.audio_stream(request, slug="story", audio_slug="chapter")

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */10")

    def test_s3_audio_stream_forwards_range_without_opening_file(self):
        storage = MagicMock(spec=S3Storage)
        storage.bucket_name = "audiobooks"
        response_payload = {"Body": BytesIO(b"2345"), "ContentLength": 4}
        storage.connection.meta.client.get_object.return_value = response_payload
        audio_file = MagicMock()
        audio_file.storage = storage
        audio_file.name = "story_audios/chapter.mp3"

        result = open_s3_audio_stream(audio_file, 2, 5)

        self.assertIs(result, response_payload)
        storage.connection.meta.client.get_object.assert_called_once_with(
            Bucket="audiobooks",
            Key="story_audios/chapter.mp3",
            Range="bytes=2-5",
        )
        audio_file.open.assert_not_called()


class StoryMapDataTests(APITestCase):
    def setUp(self):
        cache.clear()

    def story(self, title, country, author=None, published=True):
        return Story.objects.create(
            title=title,
            slug=slugify(title),
            country=country,
            author=author,
            is_published=published,
        )

    def map_data(self):
        return self.client.get("/api/story-map/").data

    def test_regions_include_known_assignments_and_unknown_fallback(self):
        self.story("Nepal tale", "NP")
        self.story("Irish tale", "IE")
        self.story("Unknown tale", "XX")
        self.story("Draft tale", "JP", published=False)

        countries = {row["code"]: row for row in self.map_data()["countries"]}

        self.assertEqual(countries["NP"]["region"], "Asia")
        self.assertEqual(countries["IE"]["region"], "Europe")
        self.assertEqual(countries["XX"]["region"], "Other")
        self.assertNotIn("JP", countries)

    def test_top_authors_are_ordered_limited_and_exclude_special_bylines(self):
        authors = [Author.objects.create(name=f"Writer {letter}") for letter in "ABCDE"]
        for index, author in enumerate(authors):
            for number in range(5 - index):
                self.story(f"{author.name} {number}", "NP", author)
        traditional = Author.objects.create(name="Traditional", is_traditional=True)
        house = Author.objects.create(name="WorldStories", is_house=True)
        for number in range(6):
            self.story(f"Traditional {number}", "NP", traditional)
            self.story(f"House {number}", "NP", house)

        nepal = self.map_data()["countries"][0]

        self.assertEqual(
            nepal["top_authors"],
            [{"name": author.name, "slug": author.slug} for author in authors[:4]],
        )

    def test_country_journey_matches_only_when_all_required_items_match(self):
        first = self.story("Nepal one", "NP")
        second = self.story("Nepal two", "NP")
        japan = self.story("Japan one", "JP")
        matching = StoryJourney.objects.create(
            title="Nepal Path", slug="nepal-path", type=StoryJourney.TYPE_COUNTRY, active=True, order=2
        )
        mixed = StoryJourney.objects.create(
            title="Mixed Path", slug="mixed-path", type=StoryJourney.TYPE_COUNTRY, active=True, order=1
        )
        StoryJourneyItem.objects.create(journey=matching, story=first, position=1)
        StoryJourneyItem.objects.create(journey=matching, story=second, position=2)
        StoryJourneyItem.objects.create(journey=mixed, story=first, position=1)
        StoryJourneyItem.objects.create(journey=mixed, story=japan, position=2)

        countries = {row["code"]: row for row in self.map_data()["countries"]}

        self.assertEqual(countries["NP"]["journey"], {"title": "Nepal Path", "slug": "nepal-path"})
        self.assertIsNone(countries["JP"]["journey"])

    def test_region_totals_follow_the_declared_order(self):
        self.story("Nepal one", "NP")
        self.story("Nepal two", "NP")
        self.story("Irish one", "IE")
        self.story("Kenya one", "KE")

        self.assertEqual(
            self.map_data()["regions"],
            [
                {"name": "Europe", "stories_count": 1, "countries_count": 1},
                {"name": "Asia", "stories_count": 2, "countries_count": 1},
                {"name": "Africa", "stories_count": 1, "countries_count": 1},
            ],
        )

    def test_story_list_sort_title_is_case_insensitive(self):
        self.story("zebra", "NP")
        self.story("Apple", "NP")
        self.story("banana", "NP")

        response = self.client.get("/api/stories/", {"sort": "title", "country": "NP"})

        self.assertEqual([row["title"] for row in response.data["results"]], ["Apple", "banana", "zebra"])


class ScheduledPublishingTests(SimpleTestCase):
    @patch("apps.story.api.Story.objects.published")
    def test_public_queryset_is_built_for_each_request(self, published):
        queryset = MagicMock()
        queryset.select_related.return_value.prefetch_related.return_value.order_by.return_value = queryset
        # The retrieve action adds a second prefetch_related() for tags/themes.
        queryset.prefetch_related.return_value = queryset
        published.return_value = queryset
        view = StoryViewSet()
        view.action = "retrieve"

        self.assertIs(view.get_queryset(), queryset)
        published.assert_called_once_with()
        queryset.select_related.assert_called_once_with("author", "story_type")
        prefetch_related = queryset.select_related.return_value.prefetch_related
        prefetch_related.assert_called_once()
        prefetch_args = prefetch_related.call_args.args
        # A Prefetch("audios", ...) that annotates the timed-cue flag sits
        # among plain-string lookups.
        self.assertEqual(
            (prefetch_args[0], prefetch_args[1], prefetch_args[3]),
            ("genres", "categories", "videos"),
        )
        self.assertEqual(prefetch_args[2].prefetch_through, "audios")
        # retrieve-only: annotated Prefetch for tags + themes (story-detail badges).
        tag_theme_args = queryset.prefetch_related.call_args.args
        self.assertEqual(
            {arg.prefetch_through for arg in tag_theme_args}, {"tags", "themes"}
        )

    @patch("django.db.models.Model.save")
    def test_scheduled_story_uses_schedule_date_as_site_date(self, model_save):
        # story_type given explicitly (an unsaved, in-memory instance — no DB
        # access needed to construct it) so Story.__init__ never evaluates
        # the field's DB-querying default, which SimpleTestCase forbids.
        story = Story(
            title="Scheduled",
            slug="scheduled",
            is_published=True,
            publish_at=datetime(2026, 9, 4, 10, 0, tzinfo=datetime_timezone.utc),
            story_type=StoryType(id=1, name="Short Story"),
        )

        story.save()

        self.assertEqual(story.site_published_date, date(2026, 9, 4))
        model_save.assert_called_once()

    @patch("core.urls.Page.objects.published")
    @patch("core.urls.Category.objects.annotate")
    @patch("core.urls.Genre.objects.annotate")
    @patch("core.urls.Theme.objects.annotate")
    @patch("core.urls.Tag.objects.annotate")
    @patch("core.urls.Author.objects.all")
    @patch("core.urls.Blog.objects.published")
    @patch("core.urls.Story.objects.published")
    def test_sitemap_uses_scheduled_publication_gate(
        self, published, blogs_published, authors_all, tags_annotate, themes_annotate, genres_annotate,
        categories_annotate, pages_published,
    ):
        pages_published.return_value.filter.return_value.only.return_value = []
        chapter = SimpleNamespace(slug="chapter-one")
        original = SimpleNamespace(
            slug="visible-story",
            site_published_date=date(2026, 8, 2),
            is_original=True,
            chapters=SimpleNamespace(all=lambda: [chapter]),
        )
        pd_story = SimpleNamespace(
            slug="public-domain-book",
            site_published_date=date(2026, 8, 2),
            is_original=False,
            chapters=SimpleNamespace(all=lambda: [SimpleNamespace(slug="pd-chapter-one")]),
        )
        queryset = MagicMock()
        (
            queryset.exclude.return_value.prefetch_related.return_value.only.return_value.iterator.return_value
        ) = iter([original, pd_story])
        published.return_value = queryset
        blogs_published.return_value.only.return_value.iterator.return_value = iter([])
        authors_all.return_value.only.return_value.iterator.return_value = iter([])
        for taxonomy_annotate in (tags_annotate, themes_annotate, genres_annotate, categories_annotate):
            taxonomy_annotate.return_value.filter.return_value.only.return_value.iterator.return_value = iter([])

        response = sitemap(RequestFactory().get("/api/sitemap.xml"))
        xml = response.content.decode()

        # Both stories get a /story/ entry; only the Originals title's chapter
        # pages are listed — a public-domain title's are thin/duplicate content.
        self.assertContains(response, "/story/visible-story")
        self.assertContains(response, "/story/public-domain-book")
        self.assertContains(response, "/read/visible-story/chapter-one")
        self.assertNotIn("/read/public-domain-book/pd-chapter-one", xml)
        self.assertContains(response, "/story-map")
        self.assertIn("<lastmod>2026-08-02</lastmod>", xml)
        published.assert_called_once_with()
        queryset.exclude.assert_called_once_with(story_type__name="Summary")
        queryset.exclude.return_value.prefetch_related.return_value.only.assert_called_once_with(
            "slug", "site_published_date", "is_original"
        )


class BlogModelTests(TestCase):
    def test_published_excludes_unpublished_and_future_scheduled(self):
        Blog.objects.create(title="Live", slug="live", content="<p>x</p>")
        Blog.objects.create(title="Draft", slug="draft", content="<p>x</p>", is_published=False)
        Blog.objects.create(
            title="Future", slug="future", content="<p>x</p>",
            publish_at=timezone.now() + timedelta(days=1),
        )
        Blog.objects.create(
            title="Past", slug="past", content="<p>x</p>",
            publish_at=timezone.now() - timedelta(days=1),
        )

        published_slugs = set(Blog.objects.published().values_list("slug", flat=True))

        self.assertEqual(published_slugs, {"live", "past"})

    def test_linked_story_relationship_is_removed_when_story_is_deleted(self):
        story = Story.objects.create(title="Book", slug="book")
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>")
        blog.linked_stories.add(story)

        story.delete()

        self.assertFalse(blog.linked_stories.exists())

    def test_sitemap_includes_only_published_blog_posts(self):
        Blog.objects.create(title="Live Post", slug="live-post", content="<p>x</p>")
        Blog.objects.create(title="Draft Post", slug="draft-post", content="<p>x</p>", is_published=False)
        Blog.objects.create(
            title="Scheduled Post", slug="scheduled-post", content="<p>x</p>",
            publish_at=timezone.now() + timedelta(days=1),
        )

        response = sitemap(RequestFactory().get("/api/sitemap.xml"))

        self.assertContains(response, "/blog/live-post")
        self.assertNotContains(response, "/blog/draft-post")
        self.assertNotContains(response, "/blog/scheduled-post")

    def test_sitemap_lists_chapter_pages_only_for_worldstories_originals(self):
        pd_story = Story.objects.create(
            title="A Public Domain Novel", slug="pd-novel", is_published=True, is_original=False
        )
        Chapter.objects.create(story=pd_story, title="Chapter 1", slug="pd-ch-1", content="<p>x</p>", order=1)
        original = Story.objects.create(
            title="The Repair Shop", slug="the-repair-shop", is_published=True, is_original=True
        )
        Chapter.objects.create(story=original, title="Chapter 1", slug="orig-ch-1", content="<p>x</p>", order=1)

        response = sitemap(RequestFactory().get("/api/sitemap.xml"))
        xml = response.content.decode()

        # Story-level pages listed for both, regardless of flag.
        self.assertIn("/story/pd-novel", xml)
        self.assertIn("/story/the-repair-shop", xml)
        # Chapter pages: Originals only.
        self.assertIn("/read/the-repair-shop/orig-ch-1", xml)
        self.assertNotIn("/read/pd-novel/pd-ch-1", xml)


class BlogAdminApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="blogadmin@example.com", username="blogadmin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_create_generates_unique_slug_from_title(self):
        self.client.force_authenticate(user=self._make_superuser())
        Blog.objects.create(title="Existing", slug="my-post", content="<p>x</p>")

        response = self.client.post(
            "/api/admin/blog/", {"title": "My Post", "content": "<p>New content</p>"}, format="json"
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["slug"], "my-post-2")

    def test_update_preserves_explicit_slug(self):
        self.client.force_authenticate(user=self._make_superuser())
        blog = Blog.objects.create(title="Title", slug="original-slug", content="<p>x</p>")

        response = self.client.patch(
            f"/api/admin/blog/{blog.id}/", {"slug": "custom-slug"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["slug"], "custom-slug")

    def test_create_with_cover_image_and_linked_story(self):
        self.client.force_authenticate(user=self._make_superuser())
        story = Story.objects.create(title="Linked Book", slug="linked-book")
        # VersatileImageField validates the upload is a real decodable image
        # (unlike a direct .create() call, which bypasses field validation) —
        # a real minimal JPEG is needed here, not placeholder bytes.
        from PIL import Image

        image_buffer = BytesIO()
        Image.new("RGB", (10, 10), color="red").save(image_buffer, format="JPEG")
        image = SimpleUploadedFile("cover.jpg", image_buffer.getvalue(), content_type="image/jpeg")

        response = self.client.post(
            "/api/admin/blog/",
            {
                "title": "A Post",
                "content": "<p>Body</p>",
                "excerpt": "Short teaser",
                "author_name": "Jane Doe",
                "linked_stories": [story.id],
                "cover_image_file": image,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["linked_stories"], [story.id])
        self.assertEqual(response.data["linked_story_details"][0]["slug"], "linked-book")
        self.assertTrue(response.data["cover_image_url"])

    def test_create_and_update_with_multiple_linked_stories_and_blogs(self):
        self.client.force_authenticate(user=self._make_superuser())
        stories = [
            Story.objects.create(title="Book A", slug="book-a"),
            Story.objects.create(title="Book B", slug="book-b"),
        ]
        related_blogs = [
            Blog.objects.create(title="Related A", slug="related-a", content="<p>x</p>"),
            Blog.objects.create(title="Related B", slug="related-b", content="<p>x</p>"),
        ]

        response = self.client.post(
            "/api/admin/blog/",
            {
                "title": "Post",
                "content": "<p>Body</p>",
                "linked_stories": [story.id for story in stories],
                "linked_blogs": [blog.id for blog in related_blogs],
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertCountEqual(response.data["linked_stories"], [story.id for story in stories])
        self.assertCountEqual(response.data["linked_blogs"], [blog.id for blog in related_blogs])

        clear_response = self.client.patch(
            f"/api/admin/blog/{response.data['id']}/",
            {"linked_stories": "", "linked_blogs": ""},
            format="multipart",
        )

        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(clear_response.data["linked_stories"], [])
        self.assertEqual(clear_response.data["linked_blogs"], [])

    def test_blog_cannot_link_to_itself(self):
        self.client.force_authenticate(user=self._make_superuser())
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>")

        response = self.client.patch(
            f"/api/admin/blog/{blog.id}/", {"linked_blogs": [blog.id]}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("linked_blogs", response.data)

    def test_copy_cover_from_story(self):
        self.client.force_authenticate(user=self._make_superuser())
        from PIL import Image

        image_buffer = BytesIO()
        Image.new("RGB", (10, 10), color="blue").save(image_buffer, format="JPEG")
        story = Story.objects.create(
            title="Story With Cover", slug="story-with-cover",
            cover_image_file=SimpleUploadedFile("story-cover.jpg", image_buffer.getvalue(), content_type="image/jpeg"),
        )

        response = self.client.post(
            "/api/admin/blog/",
            {"title": "Post", "content": "<p>x</p>", "copy_cover_from_story": story.id},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["cover_image_url"])
        blog = Blog.objects.get(pk=response.data["id"])
        self.assertTrue(blog.cover_image_file)
        # Independent copy, not a reference to the story's own file.
        self.assertNotEqual(blog.cover_image_file.name, story.cover_image_file.name)

    def test_copy_cover_from_story_with_no_cover_is_a_no_op(self):
        self.client.force_authenticate(user=self._make_superuser())
        story = Story.objects.create(title="No Cover Story", slug="no-cover-story")

        response = self.client.post(
            "/api/admin/blog/",
            {"title": "Post", "content": "<p>x</p>", "copy_cover_from_story": story.id},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["cover_image_url"])

    def test_remove_cover_image_file(self):
        self.client.force_authenticate(user=self._make_superuser())
        image = SimpleUploadedFile("cover.jpg", b"fake-image-bytes", content_type="image/jpeg")
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>", cover_image_file=image)

        response = self.client.patch(
            f"/api/admin/blog/{blog.id}/", {"remove_cover_image_file": "true"}, format="multipart"
        )

        self.assertEqual(response.status_code, 200)
        blog.refresh_from_db()
        self.assertFalse(blog.cover_image_file)

    def test_clearing_publish_at_via_multipart_empty_string(self):
        self.client.force_authenticate(user=self._make_superuser())
        blog = Blog.objects.create(
            title="Post", slug="post", content="<p>x</p>", publish_at=timezone.now() + timedelta(days=1)
        )

        response = self.client.patch(f"/api/admin/blog/{blog.id}/", {"publish_at": ""}, format="multipart")

        self.assertEqual(response.status_code, 200)
        blog.refresh_from_db()
        self.assertIsNone(blog.publish_at)

    def test_delete(self):
        self.client.force_authenticate(user=self._make_superuser())
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>")

        response = self.client.delete(f"/api/admin/blog/{blog.id}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Blog.objects.filter(pk=blog.id).exists())

    def test_gated_to_superusers(self):
        regular_user = User.objects.create(
            email="regularblog@example.com", username="regularblog", is_superuser=False, is_active=True
        )
        self.client.force_authenticate(user=regular_user)

        response = self.client.get("/api/admin/blog/")

        self.assertEqual(response.status_code, 403)

        stats_response = self.client.get("/api/admin/blog/stats/")
        self.assertEqual(stats_response.status_code, 403)

    def test_status_filter_matches_public_visibility(self):
        self.client.force_authenticate(user=self._make_superuser())
        now = timezone.now()
        live_immediate = Blog.objects.create(title="Live Now", slug="live-now", content="<p>x</p>", is_published=True)
        live_past = Blog.objects.create(
            title="Live Past",
            slug="live-past",
            content="<p>x</p>",
            is_published=True,
            publish_at=now - timedelta(hours=2),
        )
        scheduled = Blog.objects.create(
            title="Future Post",
            slug="future-post",
            content="<p>x</p>",
            is_published=True,
            publish_at=now + timedelta(days=2),
        )
        draft = Blog.objects.create(title="Draft Post", slug="draft-post", content="<p>x</p>", is_published=False)
        draft_future = Blog.objects.create(
            title="Draft Future",
            slug="draft-future",
            content="<p>x</p>",
            is_published=False,
            publish_at=now + timedelta(days=3),
        )

        pub = self.client.get("/api/admin/blog/?status=published")
        self.assertEqual(pub.status_code, 200)
        self.assertCountEqual([item["id"] for item in pub.data["results"]], [live_immediate.id, live_past.id])

        sched = self.client.get("/api/admin/blog/?status=scheduled")
        self.assertEqual(sched.status_code, 200)
        self.assertEqual([item["id"] for item in sched.data["results"]], [scheduled.id])

        drafts = self.client.get("/api/admin/blog/?status=draft")
        self.assertEqual(drafts.status_code, 200)
        self.assertCountEqual([item["id"] for item in drafts.data["results"]], [draft.id, draft_future.id])

    def test_needs_and_topic_filters(self):
        self.client.force_authenticate(user=self._make_superuser())
        topic = BlogTopic.objects.create(name="Craft", slug="craft")
        story = Story.objects.create(title="Linked Story", slug="linked-story-filter")

        complete = Blog.objects.create(
            title="Complete",
            slug="complete",
            excerpt="Has excerpt",
            content="<p>x</p>",
            cover_image_file="blog_covers/cover.jpg",
        )
        complete.topics.add(topic)
        complete.linked_stories.add(story)

        whitespace_excerpt = Blog.objects.create(
            title="Whitespace Excerpt",
            slug="whitespace-excerpt",
            excerpt="   \t\n ",
            content="<p>x</p>",
        )
        null_excerpt = Blog.objects.create(
            title="Null Excerpt",
            slug="null-excerpt",
            excerpt=None,
            content="<p>x</p>",
        )

        no_excerpt = self.client.get("/api/admin/blog/?has_excerpt=false")
        self.assertEqual(no_excerpt.status_code, 200)
        self.assertCountEqual([r["id"] for r in no_excerpt.data["results"]], [whitespace_excerpt.id, null_excerpt.id])

        with_excerpt = self.client.get("/api/admin/blog/?has_excerpt=true")
        self.assertEqual([r["id"] for r in with_excerpt.data["results"]], [complete.id])

        no_topics = self.client.get("/api/admin/blog/?has_topics=false")
        self.assertCountEqual([r["id"] for r in no_topics.data["results"]], [whitespace_excerpt.id, null_excerpt.id])

        by_topic = self.client.get(f"/api/admin/blog/?topic={topic.id}")
        self.assertEqual([r["id"] for r in by_topic.data["results"]], [complete.id])

        no_cover = self.client.get("/api/admin/blog/?has_cover=false")
        self.assertCountEqual([r["id"] for r in no_cover.data["results"]], [whitespace_excerpt.id, null_excerpt.id])

        no_stories = self.client.get("/api/admin/blog/?has_linked_stories=false")
        self.assertCountEqual([r["id"] for r in no_stories.data["results"]], [whitespace_excerpt.id, null_excerpt.id])

    def test_sort_and_unknown_fallback(self):
        self.client.force_authenticate(user=self._make_superuser())
        now = timezone.now()
        a = Blog.objects.create(title="Alpha", slug="alpha", content="<p>x</p>")
        b = Blog.objects.create(title="Zulu", slug="zulu", content="<p>x</p>")
        Blog.objects.filter(pk=a.pk).update(created_at=now - timedelta(days=2), updated_at=now)
        Blog.objects.filter(pk=b.pk).update(created_at=now - timedelta(days=1), updated_at=now - timedelta(days=1))

        newest = self.client.get("/api/admin/blog/?sort=newest")
        self.assertEqual([r["id"] for r in newest.data["results"]], [b.id, a.id])

        oldest = self.client.get("/api/admin/blog/?sort=oldest")
        self.assertEqual([r["id"] for r in oldest.data["results"]], [a.id, b.id])

        updated = self.client.get("/api/admin/blog/?sort=updated")
        self.assertEqual([r["id"] for r in updated.data["results"]], [a.id, b.id])

        title_asc = self.client.get("/api/admin/blog/?sort=title_asc")
        self.assertEqual([r["id"] for r in title_asc.data["results"]], [a.id, b.id])

        title_desc = self.client.get("/api/admin/blog/?sort=title_desc")
        self.assertEqual([r["id"] for r in title_desc.data["results"]], [b.id, a.id])

        unknown = self.client.get("/api/admin/blog/?sort=bogus")
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual([r["id"] for r in unknown.data["results"]], [b.id, a.id])

    def test_list_omits_content_and_retrieve_includes_it(self):
        self.client.force_authenticate(user=self._make_superuser())
        blog = Blog.objects.create(title="Heavy Post", slug="heavy-post", content="<p>Long body</p>")

        list_resp = self.client.get("/api/admin/blog/")
        self.assertEqual(list_resp.status_code, 200)
        row = list_resp.data["results"][0]
        self.assertNotIn("content", row)
        self.assertIn("updated_at", row)
        self.assertIn("topic_details", row)

        detail_resp = self.client.get(f"/api/admin/blog/{blog.id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.data["content"], "<p>Long body</p>")

    def test_stats_endpoint_counts(self):
        self.client.force_authenticate(user=self._make_superuser())
        now = timezone.now()
        topic = BlogTopic.objects.create(name="Essays", slug="essays")
        story = Story.objects.create(title="Stats Story", slug="stats-story")

        pub = Blog.objects.create(
            title="Published Complete",
            slug="pub-complete",
            excerpt="Good excerpt",
            content="<p>x</p>",
            is_published=True,
            cover_image_file="blog_covers/stats-cover.jpg",
        )
        pub.topics.add(topic)
        pub.linked_stories.add(story)

        Blog.objects.create(
            title="Scheduled Missing",
            slug="sched-missing",
            excerpt="   ",
            content="<p>x</p>",
            is_published=True,
            publish_at=now + timedelta(days=1),
        )
        Blog.objects.create(
            title="Draft Missing",
            slug="draft-missing",
            excerpt="",
            content="<p>x</p>",
            is_published=False,
        )

        response = self.client.get("/api/admin/blog/stats/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            {
                "total": 3,
                "published": 1,
                "draft": 1,
                "scheduled": 1,
                "missing_excerpt": 2,
                "missing_topics": 2,
                "missing_cover": 2,
                "no_linked_stories": 2,
            },
        )


class BlogPublicApiTests(APITestCase):
    def test_list_only_returns_published_posts(self):
        Blog.objects.create(title="Live", slug="live", content="<p>x</p>")
        Blog.objects.create(title="Draft", slug="draft", content="<p>x</p>", is_published=False)

        response = self.client.get("/api/blog/")

        self.assertEqual(response.status_code, 200)
        slugs = [item["slug"] for item in response.data["results"]]
        self.assertEqual(slugs, ["live"])

    def test_search_filters_by_title_and_excerpt(self):
        Blog.objects.create(title="Dragons and Magic", slug="dragons", content="<p>x</p>")
        Blog.objects.create(title="Unrelated", slug="unrelated", content="<p>x</p>", excerpt="mentions dragons too")
        Blog.objects.create(title="Something Else", slug="else", content="<p>x</p>")

        response = self.client.get("/api/blog/?search=dragons")

        slugs = {item["slug"] for item in response.data["results"]}
        self.assertEqual(slugs, {"dragons", "unrelated"})

    def test_sort_oldest_reorders(self):
        first = Blog.objects.create(title="First", slug="first", content="<p>x</p>")
        Blog.objects.create(title="Second", slug="second", content="<p>x</p>")

        response = self.client.get("/api/blog/?sort=oldest")

        self.assertEqual(response.data["results"][0]["slug"], first.slug)

    def test_linked_to_story_filter(self):
        story = Story.objects.create(title="Book", slug="book")
        linked_blog = Blog.objects.create(title="Linked", slug="linked", content="<p>x</p>")
        linked_blog.linked_stories.add(story)
        Blog.objects.create(title="General", slug="general", content="<p>x</p>")

        linked_response = self.client.get("/api/blog/?linked_to_story=true")
        general_response = self.client.get("/api/blog/?linked_to_story=false")

        self.assertEqual([i["slug"] for i in linked_response.data["results"]], ["linked"])
        self.assertEqual([i["slug"] for i in general_response.data["results"]], ["general"])

    def test_linked_story_slug_filter_scopes_to_one_story(self):
        story_a = Story.objects.create(title="Book A", slug="book-a")
        story_b = Story.objects.create(title="Book B", slug="book-b")
        blog_a = Blog.objects.create(title="For A", slug="for-a", content="<p>x</p>")
        blog_a.linked_stories.add(story_a)
        blog_b = Blog.objects.create(title="For B", slug="for-b", content="<p>x</p>")
        blog_b.linked_stories.add(story_b)
        Blog.objects.create(title="Unlinked", slug="unlinked", content="<p>x</p>")

        response = self.client.get("/api/blog/?linked_story=book-a")

        self.assertEqual([i["slug"] for i in response.data["results"]], ["for-a"])

    def test_detail_404s_for_unpublished_slug(self):
        Blog.objects.create(title="Draft", slug="draft", content="<p>x</p>", is_published=False)

        response = self.client.get("/api/blog/draft/")

        self.assertEqual(response.status_code, 404)

    def test_detail_includes_linked_story_summary(self):
        story = Story.objects.create(title="Book Title", slug="book-title")
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>")
        blog.linked_stories.add(story)

        response = self.client.get("/api/blog/post/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["linked_stories"][0]["slug"], "book-title")
        self.assertEqual(response.data["linked_stories"][0]["title"], "Book Title")

    def test_detail_includes_only_published_linked_content(self):
        live_story = Story.objects.create(title="Live Book", slug="live-book")
        draft_story = Story.objects.create(title="Draft Book", slug="draft-book", is_published=False)
        live_blog = Blog.objects.create(title="Live Related", slug="live-related", content="<p>x</p>")
        draft_blog = Blog.objects.create(
            title="Draft Related", slug="draft-related", content="<p>x</p>", is_published=False
        )
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>")
        blog.linked_stories.add(live_story, draft_story)
        blog.linked_blogs.add(live_blog, draft_blog)

        response = self.client.get("/api/blog/post/")

        self.assertEqual([item["slug"] for item in response.data["linked_stories"]], ["live-book"])
        self.assertEqual([item["slug"] for item in response.data["linked_blogs"]], ["live-related"])

    def test_detail_includes_updated_at_for_date_modified(self):
        Blog.objects.create(title="Post", slug="post", content="<p>x</p>")

        response = self.client.get("/api/blog/post/")

        self.assertTrue(response.data["updated_at"])

    def test_detail_linked_content_is_empty_when_absent(self):
        Blog.objects.create(title="Post", slug="post", content="<p>x</p>")

        response = self.client.get("/api/blog/post/")

        self.assertEqual(response.data["linked_stories"], [])
        self.assertEqual(response.data["linked_blogs"], [])


class BecauseFinishedApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="finisher@example.com", username="finisher", password="test-password"
        )
        self.genre = Genre.objects.create(name="Mystery")
        self.other_genre = Genre.objects.create(name="Romance")

        self.finished_story = Story.objects.create(
            title="The Finished Book", slug="the-finished-book", is_published=True
        )
        self.finished_story.genres.add(self.genre)

        self.same_genre_story = Story.objects.create(
            title="Another Mystery", slug="another-mystery", is_published=True
        )
        self.same_genre_story.genres.add(self.genre)

        self.favorited_same_genre_story = Story.objects.create(
            title="Already Read Mystery", slug="already-read-mystery", is_published=True
        )
        self.favorited_same_genre_story.genres.add(self.genre)

        self.unrelated_story = Story.objects.create(
            title="Unrelated Romance", slug="unrelated-romance", is_published=True
        )
        self.unrelated_story.genres.add(self.other_genre)

    def test_requires_authentication(self):
        response = self.client.get(reverse("story-because-finished", args=[self.finished_story.slug]))

        self.assertEqual(response.status_code, 401)

    def test_recommends_stories_anchored_to_the_finished_story(self):
        Favorite.objects.create(story=self.favorited_same_genre_story, user=self.user)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("story-because-finished", args=[self.finished_story.slug]))

        self.assertEqual(response.status_code, 200)
        slugs = [story["slug"] for story in response.data]
        self.assertIn(self.same_genre_story.slug, slugs)
        self.assertNotIn(self.finished_story.slug, slugs)
        self.assertNotIn(self.favorited_same_genre_story.slug, slugs)


class PublicAuthorApiTests(APITestCase):
    def setUp(self):
        cache.clear()  # /api/discover/ is cache_page-wrapped — see HomeDataQuickReadsTests
        self.visible_author = Author.objects.create(
            name="Visible Writer",
            bio="Writes public stories.",
            image="https://example.com/writer.jpg",
        )
        self.hidden_author = Author.objects.create(name="Draft Writer")
        Story.objects.create(
            title="Published Book",
            slug="published-book",
            author=self.visible_author,
            is_published=True,
        )
        Story.objects.create(
            title="Draft Book",
            slug="draft-book",
            author=self.visible_author,
            is_published=False,
        )
        Story.objects.create(
            title="Hidden Author Draft",
            slug="hidden-author-draft",
            author=self.hidden_author,
            is_published=False,
        )

    def test_list_excludes_authors_without_published_stories_by_default(self):
        response = self.client.get(reverse("author-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pagination"]["count"], 1)
        counts = {author["id"]: author["stories_count"] for author in response.data["results"]}
        self.assertEqual(counts[self.visible_author.id], 1)

        included = self.client.get(reverse("author-list"), {"include_empty": "1"})
        included_counts = {author["id"]: author["stories_count"] for author in included.data["results"]}
        self.assertEqual(included_counts[self.hidden_author.id], 0)

    def test_detail_only_includes_published_stories(self):
        response = self.client.get(reverse("author-detail", args=[self.visible_author.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["stories"]], ["published-book"])

    def test_author_without_public_stories_requires_include_empty(self):
        response = self.client.get(reverse("author-detail", args=[self.hidden_author.id]))

        self.assertEqual(response.status_code, 404)

        included = self.client.get(
            reverse("author-detail", args=[self.hidden_author.id]),
            {"include_empty": "1"},
        )
        self.assertEqual(included.status_code, 200)
        self.assertEqual(included.data["stories_count"], 0)
        self.assertEqual(included.data["stories"], [])

    def test_story_detail_recommends_public_similar_titles_only(self):
        genre = Genre.objects.create(name="Folklore")
        current = Story.objects.get(slug="published-book")
        current.genres.add(genre)
        similar = Story.objects.create(
            title="Similar Published Book",
            slug="similar-published-book",
            author=self.hidden_author,
            story_type=current.story_type,
            language=current.language,
            is_published=True,
        )
        similar.genres.add(genre)
        draft = Story.objects.create(
            title="Similar Draft",
            slug="similar-draft",
            story_type=current.story_type,
            language=current.language,
            is_published=False,
        )
        draft.genres.add(genre)
        translation = Story.objects.create(
            title="Published Translation",
            slug="published-translation",
            translation_group=current.translation_group,
            language="es",
            is_published=True,
        )
        translation.genres.add(genre)

        response = self.client.get(reverse("story-detail", args=[current.slug]))

        self.assertEqual(response.status_code, 200)
        slugs = [story["slug"] for story in response.data["similar_stories"]]
        self.assertIn(similar.slug, slugs)
        self.assertNotIn(current.slug, slugs)
        self.assertNotIn(draft.slug, slugs)
        self.assertNotIn(translation.slug, slugs)

    def test_story_detail_serializes_tags_and_themes_with_published_counts(self):
        current = Story.objects.get(slug="published-book")
        shared_tag = Tag.objects.create(name="Adventure", slug="adventure")
        solo_theme = Theme.objects.create(name="Coming of Age", slug="coming-of-age")
        current.tags.add(shared_tag)
        current.themes.add(solo_theme)
        other = Story.objects.create(title="Also Adventure", slug="also-adventure", is_published=True)
        other.tags.add(shared_tag)
        draft = Story.objects.create(title="Draft Adventure", slug="draft-adv", is_published=False)
        draft.tags.add(shared_tag)

        response = self.client.get(reverse("story-detail", args=[current.slug]))

        self.assertEqual(response.status_code, 200)
        tags = {t["slug"]: t for t in response.data["tags"]}
        themes = {t["slug"]: t for t in response.data["themes"]}
        # shared_tag: this story + one other published (the draft doesn't count)
        self.assertEqual(tags[shared_tag.slug]["stories_count"], 2)
        # solo_theme: only this story
        self.assertEqual(themes[solo_theme.slug]["stories_count"], 1)

    def test_story_detail_marks_youtube_shorts_as_portrait(self):
        current = Story.objects.get(slug="published-book")
        Video.objects.create(
            story=current,
            title="Portrait narration",
            slug="portrait-narration",
            youtube_url="https://www.youtube.com/shorts/dQw4w9WgXcQ",
            youtube_id="dQw4w9WgXcQ",
        )

        response = self.client.get(reverse("story-detail", args=[current.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["videos"][0]["aspect_ratio"], "9:16")

    def test_story_detail_query_count_is_flat_across_tag_and_theme_counts(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        current = Story.objects.get(slug="published-book")

        def add_tags(count, prefix):
            for i in range(count):
                tag = Tag.objects.create(name=f"{prefix} Tag {i}", slug=f"{prefix}-tag-{i}")
                theme = Theme.objects.create(name=f"{prefix} Theme {i}", slug=f"{prefix}-theme-{i}")
                current.tags.add(tag)
                current.themes.add(theme)
                other = Story.objects.create(
                    title=f"{prefix} {i}", slug=f"{prefix}-{i}", is_published=True
                )
                other.tags.add(tag)
                other.themes.add(theme)

        add_tags(3, "a")
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("story-detail", args=[current.slug]))

        add_tags(6, "b")
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(reverse("story-detail", args=[current.slug]))

        # Tripling the tag/theme count doesn't grow the query count — the
        # prefetched Tag/Theme querysets carry published_stories_count, so
        # there's no per-badge COUNT. (A tiny tolerance covers unrelated
        # similar-stories prefetch variance; an N+1 would be ~+18 here.)
        self.assertLessEqual(
            len(many.captured_queries), len(few.captured_queries) + 3
        )
        self.assertEqual(len(response.data["tags"]), 9)
        self.assertEqual(len(response.data["themes"]), 9)

    def test_search_returns_authors_and_titles_in_separate_sections(self):
        response = self.client.get(reverse("search-data"), {"q": "Visible Writer"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [author["id"] for author in response.data["authors"]["results"]],
            [self.visible_author.id],
        )
        self.assertEqual(
            [story["slug"] for story in response.data["titles"]["results"]],
            ["published-book"],
        )
        self.assertEqual(response.data["authors"]["results"][0]["stories_count"], 1)

    def test_title_search_does_not_return_drafts(self):
        response = self.client.get(reverse("search-data"), {"q": "Book"})

        self.assertEqual(response.status_code, 200)
        slugs = [story["slug"] for story in response.data["titles"]["results"]]
        self.assertIn("published-book", slugs)
        self.assertNotIn("draft-book", slugs)

    def test_search_finds_a_chapter_by_title(self):
        published = Story.objects.get(slug="published-book")
        Chapter.objects.create(story=published, title="A Storm at Sea", slug="a-storm-at-sea", content="<p>Calm waters.</p>", order=1)

        response = self.client.get(reverse("search-data"), {"q": "Storm"})

        self.assertEqual(response.status_code, 200)
        results = response.data["chapters"]["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["chapter_title"], "A Storm at Sea")
        self.assertEqual(results[0]["chapter_slug"], "a-storm-at-sea")
        self.assertEqual(results[0]["story_slug"], "published-book")

    def test_search_finds_a_chapter_by_content_and_centers_the_excerpt(self):
        published = Story.objects.get(slug="published-book")
        words_before = " ".join(f"word{i}" for i in range(40))
        Chapter.objects.create(
            story=published, title="Chapter One", slug="chapter-one",
            content=f"<p>{words_before} a rare golden compass appeared suddenly.</p>", order=1,
        )

        response = self.client.get(reverse("search-data"), {"q": "golden compass"})

        results = response.data["chapters"]["results"]
        self.assertEqual(len(results), 1)
        self.assertIn("golden compass", results[0]["excerpt"])
        self.assertNotIn("word0 word1", results[0]["excerpt"])  # excerpt is centered on the match, not the start

    def test_chapter_search_does_not_include_chapters_from_drafts(self):
        draft = Story.objects.get(slug="draft-book")
        Chapter.objects.create(story=draft, title="Secret Chapter", slug="secret-chapter", content="hidden", order=1)

        response = self.client.get(reverse("search-data"), {"q": "Secret"})

        self.assertEqual(response.data["chapters"]["results"], [])

    def test_story_list_can_filter_by_story_type(self):
        novel_type = StoryType.objects.get(name="Novel")
        Story.objects.create(
            title="Published Novel",
            slug="published-novel",
            story_type=novel_type,
            is_published=True,
        )

        response = self.client.get(reverse("story-list"), {"story_type": novel_type.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["results"]], ["published-novel"])

    def test_story_list_can_filter_by_is_original(self):
        Story.objects.create(
            title="Flagship", slug="flagship", is_published=True, is_original=True
        )
        Story.objects.create(
            title="Community Story", slug="community-story", is_published=True, is_original=False
        )

        response = self.client.get(reverse("story-list"), {"is_original": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([s["slug"] for s in response.data["results"]], ["flagship"])
        self.assertTrue(response.data["results"][0]["is_original"])

        response = self.client.get(reverse("story-list"), {"is_original": "false"})
        false_slugs = [s["slug"] for s in response.data["results"]]
        self.assertIn("community-story", false_slugs)
        self.assertNotIn("flagship", false_slugs)

    def test_story_list_can_filter_by_has_audio(self):
        with_audio = Story.objects.create(
            title="Story With Audio",
            slug="story-with-audio",
            is_published=True,
        )
        Audio.objects.create(
            story=with_audio,
            title="Chapter 1",
            slug="chapter-1",
            order=1,
            audio_file="story_audios/fake.mp3",
        )
        Story.objects.create(
            title="Story Without Audio",
            slug="story-without-audio",
            is_published=True,
        )

        response = self.client.get(reverse("story-list"), {"has_audio": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["results"]], ["story-with-audio"])

    def test_story_list_can_filter_by_has_video(self):
        with_video = Story.objects.create(
            title="Story With Video",
            slug="story-with-video",
            is_published=True,
        )
        Video.objects.create(
            story=with_video,
            title="Narration 1",
            slug="narration-1",
            order=1,
            youtube_url="https://youtu.be/dQw4w9WgXcQ",
            youtube_id="dQw4w9WgXcQ",
        )
        Story.objects.create(
            title="Story Without Video",
            slug="story-without-video",
            is_published=True,
        )

        response = self.client.get(reverse("story-list"), {"has_video": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["results"]], ["story-with-video"])

    def test_story_list_video_recent_sort_orders_by_newest_video(self):
        # Published later, but its video is older — must sort after.
        newer_story = Story.objects.create(
            title="Newer Story",
            slug="newer-story",
            is_published=True,
            site_published_date=timezone.now(),
        )
        older_story = Story.objects.create(
            title="Older Story",
            slug="older-story",
            is_published=True,
            site_published_date=timezone.now() - timedelta(days=30),
        )
        for story, days_ago in ((newer_story, 10), (older_story, 1)):
            video = Video.objects.create(
                story=story,
                title=f"{story.title} video",
                slug=f"{story.slug}-video",
                order=1,
                youtube_url="https://youtu.be/dQw4w9WgXcQ",
                youtube_id="dQw4w9WgXcQ",
            )
            Video.objects.filter(pk=video.pk).update(created_at=timezone.now() - timedelta(days=days_ago))

        response = self.client.get(reverse("story-list"), {"has_video": "true", "sort": "video_recent"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [story["slug"] for story in response.data["results"]],
            ["older-story", "newer-story"],
        )

    def test_story_list_can_filter_by_has_summary(self):
        Story.objects.create(
            title="Story With Summary",
            slug="story-with-summary",
            summary="<p>" + " ".join(["word"] * 50) + "</p>",
            is_published=True,
        )
        Story.objects.create(
            title="Story Without Summary",
            slug="story-without-summary",
            summary="",
            is_published=True,
        )

        response = self.client.get(reverse("story-list"), {"has_summary": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["results"]], ["story-with-summary"])

    def test_nepali_site_flag_filters_the_public_list(self):
        flagged = Story.objects.get(slug="published-book")
        flagged.show_in_nepali_site = True
        flagged.save(update_fields=["show_in_nepali_site"])

        response = self.client.get(reverse("story-list"), {"show_in_nepali_site": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([s["slug"] for s in response.data["results"]], [flagged.slug])

    def test_nepali_site_flag_is_independent_of_language(self):
        """A Nepali story is not on the site unless it is flagged, and a
        non-Nepali story is if it is — the two are separate questions."""
        nepali_unflagged = Story.objects.create(
            title="नेपाली कथा",
            slug="nepali-unflagged",
            language="ne",
            is_published=True,
        )
        english_flagged = Story.objects.get(slug="published-book")
        english_flagged.show_in_nepali_site = True
        english_flagged.save(update_fields=["show_in_nepali_site"])

        response = self.client.get(reverse("story-list"), {"show_in_nepali_site": "true"})
        slugs = [s["slug"] for s in response.data["results"]]

        self.assertIn(english_flagged.slug, slugs)
        self.assertNotIn(nepali_unflagged.slug, slugs)

    def test_flagged_translation_survives_an_unflagged_sibling(self):
        """The regression this flag is most likely to hit.

        Translation collapsing keeps one row per group and prefers English.
        If the candidate set is not narrowed by the same flag, a flagged
        Nepali edition loses to its unflagged English sibling and is then
        filtered out — the story disappears from the site entirely rather
        than appearing once.
        """
        english = Story.objects.get(slug="published-book")  # unflagged, English
        nepali = Story.objects.create(
            title="नेपाली संस्करण",
            slug="nepali-edition",
            translation_group=english.translation_group,
            language="ne",
            is_published=True,
            show_in_nepali_site=True,
        )

        response = self.client.get(reverse("story-list"), {"show_in_nepali_site": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([s["slug"] for s in response.data["results"]], [nepali.slug])

    def test_unrecognised_nepali_site_value_does_not_empty_the_catalogue(self):
        """A typo must not silently return nothing — that reads as an outage."""
        unfiltered = self.client.get(reverse("story-list")).data["results"]
        response = self.client.get(reverse("story-list"), {"show_in_nepali_site": "yes"})

        self.assertEqual(len(response.data["results"]), len(unfiltered))

    def test_genre_directory_scopes_counts_to_the_nepali_site(self):
        """The filter dropdown must offer only genres with something behind it,
        and the counts beside them must be that site's counts, not global."""
        from apps.story.models import Genre

        flagged_genre = Genre.objects.create(name="Zeta Flagged", slug="zeta-flagged")
        unflagged_genre = Genre.objects.create(name="Zeta Unflagged", slug="zeta-unflagged")

        flagged = Story.objects.get(slug="published-book")
        flagged.show_in_nepali_site = True
        flagged.save(update_fields=["show_in_nepali_site"])
        flagged.genres.add(flagged_genre)

        other = Story.objects.create(
            title="Not on the Nepali site", slug="not-on-np-site", is_published=True
        )
        other.genres.add(unflagged_genre)

        unscoped = {g["slug"]: g["stories_count"] for g in self.client.get(reverse("genre-list")).data}
        scoped = {
            g["slug"]: g["stories_count"]
            for g in self.client.get(reverse("genre-list"), {"show_in_nepali_site": "true"}).data
        }

        self.assertIn("zeta-flagged", unscoped)
        self.assertIn("zeta-unflagged", unscoped)

        # A genre with no flagged stories disappears entirely rather than
        # offering a filter that returns nothing.
        self.assertIn("zeta-flagged", scoped)
        self.assertNotIn("zeta-unflagged", scoped)
        self.assertEqual(scoped["zeta-flagged"], 1)

    def test_genre_count_matches_the_story_list_it_filters(self):
        """The number on the chip has to be the number of rows you get when you
        click it — otherwise the dropdown lies."""
        from apps.story.models import Genre

        genre = Genre.objects.create(name="Zeta Counted", slug="zeta-counted")
        flagged = Story.objects.get(slug="published-book")
        flagged.show_in_nepali_site = True
        flagged.save(update_fields=["show_in_nepali_site"])
        flagged.genres.add(genre)
        unflagged = Story.objects.create(
            title="Unflagged sibling", slug="unflagged-sibling", is_published=True
        )
        unflagged.genres.add(genre)

        chip = next(
            g
            for g in self.client.get(reverse("genre-list"), {"show_in_nepali_site": "true"}).data
            if g["slug"] == "zeta-counted"
        )
        listed = self.client.get(
            reverse("story-list"),
            {"show_in_nepali_site": "true", "genres": genre.id},
        ).data["pagination"]["count"]

        self.assertEqual(chip["stories_count"], listed)

    def test_story_list_language_filter_returns_matching_translation(self):
        english = Story.objects.get(slug="published-book")
        spanish = Story.objects.create(
            title="Libro Publicado",
            slug="libro-publicado",
            translation_group=english.translation_group,
            language="es",
            is_published=True,
        )

        response = self.client.get(reverse("story-list"), {"language": "es"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["results"]], [spanish.slug])


class NepaliSitemapTests(APITestCase):
    """The Nepali site is indexed as its own Search Console property, so this
    sitemap must contain only its own URLs, on its own domain."""

    def setUp(self):
        self.url = reverse("nepali-sitemap")
        self.flagged = Story.objects.create(
            title="फ्ल्याग गरिएको",
            slug="np-flagged",
            is_published=True,
            show_in_nepali_site=True,
        )
        self.unflagged = Story.objects.create(
            title="Not flagged", slug="np-unflagged", is_published=True
        )

    # Django's default mail_admins handler fires on any 5xx once DEBUG is
    # False (as it is under the test runner) and renders the debug traceback
    # template, which trips a copy() incompatibility in Django 5.0 on Python
    # 3.14. Unrelated to this view — muted so the assertion can run.
    @patch("django.utils.log.AdminEmailHandler.emit")
    def test_refuses_to_emit_urls_without_a_configured_domain(self, _emit):
        """A sitemap on the wrong host is worse than no sitemap — Search
        Console would index it before anyone noticed."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NP_SITE_URL", None)
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)

    def test_lists_only_flagged_stories_on_the_new_domain(self):
        with patch.dict(os.environ, {"NP_SITE_URL": "https://ne.example.test"}):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml")
        body = response.content.decode()

        self.assertIn("https://ne.example.test/katha/np-flagged", body)
        self.assertNotIn("np-unflagged", body)
        # Homepage and catalogue.
        self.assertIn("<loc>https://ne.example.test/</loc>", body)
        self.assertIn("https://ne.example.test/kathaharu", body)
        # No mother-site URL may leak in.
        self.assertNotIn("worldstories.net", body)
        self.assertNotIn("/story/", body)

    def test_paths_are_configurable_so_the_slug_decision_is_not_baked_in(self):
        with patch.dict(
            os.environ,
            {
                "NP_SITE_URL": "https://ne.example.test",
                "NP_CATALOGUE_PATH": "stories",
                "NP_STORY_PATH_PREFIX": "s",
            },
        ):
            body = self.client.get(self.url).content.decode()

        self.assertIn("https://ne.example.test/stories", body)
        self.assertIn("https://ne.example.test/s/np-flagged", body)

    def test_unpublished_and_summary_stories_are_excluded(self):
        summary_type, _ = StoryType.objects.get_or_create(name="Summary")
        Story.objects.create(
            title="Flagged summary",
            slug="np-summary",
            is_published=True,
            show_in_nepali_site=True,
            story_type=summary_type,
        )
        Story.objects.create(
            title="Flagged draft",
            slug="np-draft",
            is_published=False,
            show_in_nepali_site=True,
        )

        with patch.dict(os.environ, {"NP_SITE_URL": "https://ne.example.test"}):
            body = self.client.get(self.url).content.decode()

        self.assertNotIn("np-summary", body)
        self.assertNotIn("np-draft", body)
        self.assertIn("np-flagged", body)

    def test_the_mother_sitemap_is_unaffected(self):
        with patch.dict(os.environ, {"SITE_URL": "https://worldstories.test"}):
            body = self.client.get(reverse("sitemap")).content.decode()

        # The mother site still lists everything published, flagged or not, on
        # its own domain and its own paths.
        self.assertIn("https://worldstories.test/story/np-unflagged", body)
        self.assertIn("https://worldstories.test/story/np-flagged", body)
        self.assertNotIn("/katha/", body)


class OriginalPublicationDateValidationTests(SimpleTestCase):
    def test_rejects_impossible_calendar_date(self):
        serializer = StoryAdminSerializer()
        with self.assertRaises(ValidationError):
            serializer.validate(
                {
                    "title": "Invalid date",
                    "slug": "invalid-date",
                    "original_published_year": 2025,
                    "original_published_month": 2,
                    "original_published_day": 31,
                }
            )

    def test_accepts_partial_original_publication_date(self):
        serializer = StoryAdminSerializer()
        attrs = {
            "title": "Year only",
            "slug": "year-only",
            "original_published_year": 1920,
            "original_published_month": None,
            "original_published_day": None,
        }
        self.assertEqual(serializer.validate(attrs), attrs)


class StoryAdminMultipartValidationTests(TestCase):
    def test_uploaded_file_is_not_deep_copied_when_normalizing_empty_dates(self):
        upload = SimpleUploadedFile(
            "story.pdf",
            b"%PDF-1.4 test document",
            content_type="application/pdf",
        )
        data = QueryDict("", mutable=True)
        data["title"] = "Multipart Story"
        data["site_published_date"] = ""
        data["story_type"] = StoryType.objects.get(name="Short Story").id
        data.setlist("pdf_file", [upload])

        serializer = StoryAdminSerializer(data=data)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(serializer.validated_data["site_published_date"])
        self.assertIs(serializer.validated_data["pdf_file"], upload)


class StoryAdminListFilterTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="storyreport-admin@example.com",
            username="storyreport-admin",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.client.force_authenticate(self.admin)

        self.published_completed_with_summary = Story.objects.create(
            title="Published Completed With Summary",
            slug="published-completed-with-summary",
            is_published=True,
            is_completed=True,
            summary="<p>A summary.</p>",
        )
        self.draft_ongoing_no_summary = Story.objects.create(
            title="Draft Ongoing No Summary",
            slug="draft-ongoing-no-summary",
            is_published=False,
            is_completed=False,
        )

    def test_filters_by_publication_status(self):
        response = self.client.get(reverse("admin-story-list"), {"is_published": "true"})

        slugs = [story["slug"] for story in response.data["results"]]
        self.assertIn(self.published_completed_with_summary.slug, slugs)
        self.assertNotIn(self.draft_ongoing_no_summary.slug, slugs)

    def test_filters_by_completion_status(self):
        response = self.client.get(reverse("admin-story-list"), {"is_completed": "false"})

        slugs = [story["slug"] for story in response.data["results"]]
        self.assertIn(self.draft_ongoing_no_summary.slug, slugs)
        self.assertNotIn(self.published_completed_with_summary.slug, slugs)

    def test_filters_by_summary_presence(self):
        response = self.client.get(reverse("admin-story-list"), {"has_summary": "true"})

        slugs = [story["slug"] for story in response.data["results"]]
        self.assertIn(self.published_completed_with_summary.slug, slugs)
        self.assertNotIn(self.draft_ongoing_no_summary.slug, slugs)

    def test_filters_combine(self):
        response = self.client.get(
            reverse("admin-story-list"), {"is_published": "true", "has_summary": "false"}
        )

        self.assertEqual(response.data["results"], [])

    def test_filters_by_is_original(self):
        self.published_completed_with_summary.is_original = True
        self.published_completed_with_summary.save(update_fields=["is_original"])

        response = self.client.get(reverse("admin-story-list"), {"is_original": "true"})
        slugs = [story["slug"] for story in response.data["results"]]
        self.assertEqual(slugs, [self.published_completed_with_summary.slug])

    def test_create_and_update_persist_is_original(self):
        response = self.client.post(
            reverse("admin-story-list"),
            {
                "title": "Fresh Original",
                "story_type": StoryType.objects.get(name="Short Story").id,
                "is_original": "true",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        story = Story.objects.get(slug=response.data["slug"])
        self.assertTrue(story.is_original)

        patch = self.client.patch(
            reverse("admin-story-detail", args=[story.id]),
            {"is_original": "false"},
            format="multipart",
        )
        self.assertEqual(patch.status_code, 200, patch.data)
        story.refresh_from_db()
        self.assertFalse(story.is_original)

    def test_requires_superuser(self):
        regular_user = User.objects.create_user(
            email="storyreport-regular@example.com", username="storyreport-regular", password="x"
        )
        self.client.force_authenticate(regular_user)

        response = self.client.get(reverse("admin-story-list"))

        self.assertEqual(response.status_code, 403)


class StoryFeaturedApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="featured-admin@example.com",
            username="featured-admin",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.a = Story.objects.create(title="Story A", slug="featured-a", is_published=True)
        self.b = Story.objects.create(title="Story B", slug="featured-b", is_published=True)
        self.c = Story.objects.create(title="Story C", slug="featured-c", is_published=True)

    def test_get_is_empty_initially(self):
        response = self.client.get(reverse("admin-story-featured"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_put_sets_ranks_in_the_given_order(self):
        response = self.client.put(
            reverse("admin-story-featured"),
            {"story_ids": [self.c.id, self.a.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s["slug"] for s in response.data], ["featured-c", "featured-a"])

        self.c.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.c.featured_rank, 1)
        self.assertEqual(self.a.featured_rank, 2)

        get_response = self.client.get(reverse("admin-story-featured"))
        self.assertEqual([s["slug"] for s in get_response.data], ["featured-c", "featured-a"])

    def test_put_replaces_and_clears_removed_stories(self):
        self.client.put(
            reverse("admin-story-featured"),
            {"story_ids": [self.a.id, self.b.id, self.c.id]},
            format="json",
        )
        response = self.client.put(
            reverse("admin-story-featured"), {"story_ids": [self.b.id]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.c.refresh_from_db()
        self.assertIsNone(self.a.featured_rank)
        self.assertEqual(self.b.featured_rank, 1)
        self.assertIsNone(self.c.featured_rank)

    def test_put_rejects_more_than_five(self):
        extra = [
            Story.objects.create(title=f"Extra {i}", slug=f"featured-extra-{i}", is_published=True).id
            for i in range(3)
        ]
        response = self.client.put(
            reverse("admin-story-featured"),
            {"story_ids": [self.a.id, self.b.id, self.c.id, *extra]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Story.objects.filter(featured_rank__isnull=False).exists())

    def test_put_rejects_duplicate_story(self):
        response = self.client.put(
            reverse("admin-story-featured"),
            {"story_ids": [self.a.id, self.a.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_put_rejects_unknown_story_id(self):
        response = self.client.put(
            reverse("admin-story-featured"), {"story_ids": [999999]}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_superuser(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("admin-story-featured"))
        self.assertIn(response.status_code, (401, 403))

        regular_user = User.objects.create_user(
            email="featured-regular@example.com", username="featured-regular", password="x"
        )
        self.client.force_authenticate(regular_user)
        response = self.client.put(
            reverse("admin-story-featured"), {"story_ids": [self.a.id]}, format="json"
        )
        self.assertEqual(response.status_code, 403)


class StoryQueueApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="queue-admin@example.com",
            username="queue-admin",
            password="test-password",
            is_superuser=True,
            is_staff=True,
        )
        self.client.force_authenticate(self.admin)

    def test_requires_superuser(self):
        regular_user = User.objects.create_user(
            email="queue-regular@example.com", username="queue-regular", password="x"
        )
        self.client.force_authenticate(regular_user)

        response = self.client.get(reverse("admin-story-queue-list"))

        self.assertEqual(response.status_code, 403)

    def test_starts_empty_and_can_be_added_to(self):
        list_response = self.client.get(reverse("admin-story-queue-list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["results"], [])

        create_response = self.client.post(
            reverse("admin-story-queue-list"),
            {"title": "The Wind-Up Bird", "author_name": "Haruki Murakami"},
        )

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data["is_added"], False)
        self.assertIsNone(create_response.data["added_story"])

    def test_can_be_updated(self):
        item = StoryQueue.objects.create(title="Draft Title", author_name="Someone")

        response = self.client.patch(
            reverse("admin-story-queue-detail", args=[item.id]),
            {"title": "Final Title", "author_name": "Someone Else", "about": "Updated blurb."},
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.title, "Final Title")
        self.assertEqual(item.author_name, "Someone Else")
        self.assertEqual(item.about, "Updated blurb.")

    def test_update_cannot_tamper_with_is_added_or_added_story(self):
        added_story = Story.objects.create(title="Already Published", slug="already-published")
        item = StoryQueue.objects.create(
            title="Already Added", author_name="Someone", is_added=True, added_story=added_story
        )
        other_story = Story.objects.create(title="Someone Else's Story", slug="someone-elses-story")

        response = self.client.patch(
            reverse("admin-story-queue-detail", args=[item.id]),
            {"is_added": False, "added_story": other_story.id},
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertTrue(item.is_added)
        self.assertEqual(item.added_story_id, added_story.id)

    def test_filters_by_is_added(self):
        StoryQueue.objects.create(title="Not Added", author_name="Someone")
        added_story = Story.objects.create(title="Added Story", slug="added-story")
        StoryQueue.objects.create(
            title="Already Added", author_name="Someone Else", is_added=True, added_story=added_story
        )

        response = self.client.get(reverse("admin-story-queue-list"), {"is_added": "true"})

        titles = [item["title"] for item in response.data["results"]]
        self.assertEqual(titles, ["Already Added"])

    def test_published_date_label_formats_whichever_parts_are_known(self):
        year_only = StoryQueue.objects.create(
            title="Year Only", author_name="Someone", original_published_year=1920
        )
        full_date = StoryQueue.objects.create(
            title="Full Date",
            author_name="Someone",
            original_published_year=2002,
            original_published_month=9,
            original_published_day=12,
        )

        response = self.client.get(reverse("admin-story-queue-list"))

        labels = {item["title"]: item["published_date_label"] for item in response.data["results"]}
        self.assertEqual(labels["Year Only"], "1920")
        self.assertEqual(labels["Full Date"], "September 12, 2002")

    def test_add_action_creates_a_draft_story_and_flips_is_added(self):
        queue_item = StoryQueue.objects.create(title="Kafka on the Shore", author_name="Haruki Murakami")

        response = self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["is_added"], True)
        self.assertIsNotNone(response.data["added_story"])

        queue_item.refresh_from_db()
        self.assertTrue(queue_item.is_added)
        self.assertIsNotNone(queue_item.added_story)
        self.assertEqual(queue_item.added_story.title, "Kafka on the Shore")
        self.assertEqual(queue_item.added_story.author.name, "Haruki Murakami")
        self.assertFalse(queue_item.added_story.is_published)
        self.assertTrue(queue_item.added_story.slug)

    def test_add_action_carries_extended_fields_onto_the_new_story(self):
        genre = Genre.objects.create(name="Magical Realism")
        category = Category.objects.create(name="Classic Literature")
        queue_item = StoryQueue.objects.create(
            title="Kafka on the Shore",
            author_name="Haruki Murakami",
            about="A boy runs away from home.",
            story_type=StoryType.objects.get(name="Novel"),
            country="JP",
            language="ja",
            original_published_year=2002,
            original_published_month=9,
            original_published_day=12,
            cover_image_link="https://example.com/cover.jpg",
            epub_link="https://example.com/book.epub",
            pdf_link="https://example.com/book.pdf",
        )
        queue_item.genres.add(genre)
        queue_item.categories.add(category)

        response = self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        self.assertEqual(response.status_code, 201)
        queue_item.refresh_from_db()
        story = queue_item.added_story
        self.assertEqual(story.about, "A boy runs away from home.")
        self.assertEqual(story.story_type.name, "Novel")
        self.assertEqual(story.country, "JP")
        self.assertEqual(story.language, "ja")
        self.assertEqual(story.original_published_year, 2002)
        self.assertEqual(story.original_published_month, 9)
        self.assertEqual(story.original_published_day, 12)
        self.assertEqual(story.cover_image, "https://example.com/cover.jpg")
        self.assertEqual(list(story.genres.values_list("id", flat=True)), [genre.id])
        self.assertEqual(list(story.categories.values_list("id", flat=True)), [category.id])

    def test_add_action_copies_content_into_a_chapter_1(self):
        queue_item = StoryQueue.objects.create(
            title="A Short Story", author_name="Someone", content="Once upon a time..."
        )

        response = self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        self.assertEqual(response.status_code, 201)
        queue_item.refresh_from_db()
        story = queue_item.added_story
        self.assertEqual(story.chapters.count(), 1)
        chapter = story.chapters.first()
        self.assertEqual(chapter.title, "Chapter 1")
        self.assertEqual(chapter.slug, "chapter-1")
        self.assertEqual(chapter.content, "Once upon a time...")
        self.assertEqual(chapter.order, 1)

    def test_add_action_creates_no_chapter_when_content_is_blank(self):
        queue_item = StoryQueue.objects.create(title="No Content Here", author_name="Someone")

        response = self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        self.assertEqual(response.status_code, 201)
        queue_item.refresh_from_db()
        self.assertEqual(queue_item.added_story.chapters.count(), 0)

    def test_notes_can_be_saved_and_updated_but_are_never_copied_to_the_story(self):
        create_response = self.client.post(
            reverse("admin-story-queue-list"),
            {"title": "A Book", "author_name": "Someone", "notes": "<p>Found via a library sale.</p>"},
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data["notes"], "<p>Found via a library sale.</p>")

        item_id = create_response.data["id"]
        update_response = self.client.patch(
            reverse("admin-story-queue-detail", args=[item_id]), {"notes": "<p>Updated note.</p>"}
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.data["notes"], "<p>Updated note.</p>")

        add_response = self.client.post(reverse("admin-story-queue-add", args=[item_id]))
        self.assertEqual(add_response.status_code, 201)
        story = StoryQueue.objects.get(id=item_id).added_story
        self.assertFalse(hasattr(story, "notes"))

    def test_queue_item_rejects_a_month_without_a_year(self):
        response = self.client.post(
            reverse("admin-story-queue-list"),
            {"title": "Untitled", "author_name": "Someone", "original_published_month": 5},
        )

        self.assertEqual(response.status_code, 400)

    def test_add_action_reuses_an_existing_author_by_name(self):
        author = Author.objects.create(name="Haruki Murakami")
        queue_item = StoryQueue.objects.create(title="Norwegian Wood", author_name="Haruki Murakami")

        self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        queue_item.refresh_from_db()
        self.assertEqual(queue_item.added_story.author_id, author.id)
        self.assertEqual(Author.objects.filter(name="Haruki Murakami").count(), 1)

    def test_add_action_rejects_an_already_added_item(self):
        story = Story.objects.create(title="Already Added", slug="already-added")
        queue_item = StoryQueue.objects.create(
            title="Already Added", author_name="Someone", is_added=True, added_story=story
        )

        response = self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        self.assertEqual(response.status_code, 400)

    def test_queue_item_can_be_created_without_an_author(self):
        response = self.client.post(reverse("admin-story-queue-list"), {"title": "Anonymous Tale"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["author_name"], "")

    def test_add_action_creates_a_story_with_no_author_when_queue_item_has_none(self):
        queue_item = StoryQueue.objects.create(title="Anonymous Tale")

        response = self.client.post(reverse("admin-story-queue-add", args=[queue_item.id]))

        self.assertEqual(response.status_code, 201)
        queue_item.refresh_from_db()
        self.assertIsNone(queue_item.added_story.author)

    def test_check_title_finds_a_matching_published_story_case_insensitively(self):
        Story.objects.create(title="Kafka on the Shore", slug="kafka-on-the-shore", is_published=True)

        response = self.client.get(reverse("admin-story-queue-check-title"), {"title": "kafka ON the shore"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["story_matches"]), 1)
        self.assertEqual(response.data["story_matches"][0]["slug"], "kafka-on-the-shore")
        self.assertTrue(response.data["story_matches"][0]["is_published"])
        self.assertEqual(response.data["queue_matches"], [])

    def test_check_title_finds_a_not_yet_added_queue_entry(self):
        StoryQueue.objects.create(title="Norwegian Wood", author_name="Haruki Murakami")

        response = self.client.get(reverse("admin-story-queue-check-title"), {"title": "Norwegian Wood"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["story_matches"], [])
        self.assertEqual(len(response.data["queue_matches"]), 1)
        self.assertEqual(response.data["queue_matches"][0]["author_name"], "Haruki Murakami")

    def test_check_title_ignores_an_already_added_queue_entry(self):
        story = Story.objects.create(title="Already Added", slug="already-added")
        StoryQueue.objects.create(title="Already Added", is_added=True, added_story=story)

        response = self.client.get(reverse("admin-story-queue-check-title"), {"title": "Already Added"})

        # The Story-side match is enough to flag it; the queue entry that
        # produced it shouldn't also show up as a separate "in queue" hit.
        self.assertEqual(len(response.data["story_matches"]), 1)
        self.assertEqual(response.data["queue_matches"], [])

    def test_check_title_returns_nothing_for_a_blank_or_unmatched_title(self):
        response = self.client.get(reverse("admin-story-queue-check-title"), {"title": "   "})
        self.assertEqual(response.data, {"story_matches": [], "queue_matches": []})

        response = self.client.get(reverse("admin-story-queue-check-title"), {"title": "No Such Title"})
        self.assertEqual(response.data, {"story_matches": [], "queue_matches": []})

    def test_check_title_matches_by_prefix_while_still_typing(self):
        Story.objects.create(title="Kafka on the Shore", slug="kafka-on-the-shore")
        StoryQueue.objects.create(title="Kafka and the Trial", author_name="Franz Kafka")
        # Shouldn't match a title that merely contains the prefix elsewhere.
        Story.objects.create(title="A Tribute to Kafka", slug="a-tribute-to-kafka")

        response = self.client.get(reverse("admin-story-queue-check-title"), {"title": "kaf"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([m["title"] for m in response.data["story_matches"]], ["Kafka on the Shore"])
        self.assertEqual([m["title"] for m in response.data["queue_matches"]], ["Kafka and the Trial"])

    def test_check_title_excludes_the_queue_item_being_edited(self):
        item = StoryQueue.objects.create(title="The Three Musketeers", author_name="Alexandre Dumas")

        response = self.client.get(
            reverse("admin-story-queue-check-title"),
            {"title": "The Three Musketeers", "exclude_queue_id": item.id},
        )

        self.assertEqual(response.data["story_matches"], [])
        self.assertEqual(response.data["queue_matches"], [])

    def test_check_title_exclude_queue_id_still_finds_a_different_duplicate(self):
        editing_item = StoryQueue.objects.create(title="The Three Musketeers", author_name="Alexandre Dumas")
        StoryQueue.objects.create(title="The Three Musketeers", author_name="A Different Entry")

        response = self.client.get(
            reverse("admin-story-queue-check-title"),
            {"title": "The Three Musketeers", "exclude_queue_id": editing_item.id},
        )

        self.assertEqual(len(response.data["queue_matches"]), 1)
        self.assertEqual(response.data["queue_matches"][0]["author_name"], "A Different Entry")

    def test_check_title_exclude_queue_id_also_excludes_its_own_added_story(self):
        story = Story.objects.create(title="Already Added", slug="already-added")
        item = StoryQueue.objects.create(title="Already Added", is_added=True, added_story=story)

        response = self.client.get(
            reverse("admin-story-queue-check-title"),
            {"title": "Already Added", "exclude_queue_id": item.id},
        )

        self.assertEqual(response.data["story_matches"], [])
        self.assertEqual(response.data["queue_matches"], [])


class SubmissionFileUploadValidationTests(TestCase):
    """A logged-in user can submit whatever bytes they like under a
    pdf_file/epub_file field — the model previously had no extension or size
    validation, so a mislabeled or oversized upload would sail straight
    through to public R2 storage. These pin the FileExtensionValidator /
    FileSizeValidator now declared on the model fields.

    TestCase (not SimpleTestCase) since story_type is now a SlugRelatedField
    that needs a real DB lookup to validate against StoryType."""

    def _minimal_submission_data(self, **overrides):
        data = {
            "title": "Test Submission",
            "about": "A story about testing.",
            "content": "Once upon a time.",
            "story_type": "Short Story",
            "language": "en",
            "genres": [],
        }
        data.update(overrides)
        return data

    def test_rejects_pdf_file_with_the_wrong_extension(self):
        disguised = SimpleUploadedFile(
            "not-a-pdf.exe", b"MZ fake executable bytes", content_type="application/octet-stream"
        )
        serializer = SubmissionSerializer(data=self._minimal_submission_data(pdf_file=disguised))

        self.assertFalse(serializer.is_valid())
        self.assertIn("pdf_file", serializer.errors)

    def test_rejects_epub_file_over_the_size_cap(self):
        oversized = SimpleUploadedFile("book.epub", b"0" * 1024, content_type="application/epub+zip")
        oversized.size = 51 * 1024 * 1024  # 1MB over the 50MB cap, without allocating 51MB
        serializer = SubmissionSerializer(data=self._minimal_submission_data(epub_file=oversized))

        self.assertFalse(serializer.is_valid())
        self.assertIn("epub_file", serializer.errors)

    def test_accepts_a_correctly_typed_pdf_file_within_the_size_cap(self):
        valid = SimpleUploadedFile("story.pdf", b"%PDF-1.4 test document", content_type="application/pdf")
        serializer = SubmissionSerializer(data=self._minimal_submission_data(pdf_file=valid))

        self.assertTrue(serializer.is_valid(), serializer.errors)


class AudioFileUploadValidationTests(SimpleTestCase):
    def test_rejects_audio_file_with_the_wrong_extension(self):
        disguised = SimpleUploadedFile(
            "not-audio.exe", b"MZ fake executable bytes", content_type="application/octet-stream"
        )
        serializer = AudioAdminSerializer(
            data={"title": "Chapter One", "order": 1, "audio_file": disguised}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("audio_file", serializer.errors)


class ExtractChaptersTests(SimpleTestCase):
    """extract_chapters (epub_import.py) is pure — no DB/Django ORM — so
    these build small real in-memory EPUBs via ebooklib rather than mocking
    it, exercising the actual container/spine/TOC parsing path."""

    def test_well_formed_epub3_nav_orders_by_spine_and_titles_by_toc(self):
        cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml", lang="en")
        cover.content = "<html><body><h1>Cover</h1></body></html>"
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = "<html><body><h1>Intro</h1><p>First chapter body.</p></body></html>"
        c2 = epub.EpubHtml(title="c2", file_name="chap2.xhtml", lang="en")
        c2.content = "<html><body><p>Second chapter body.</p></body></html>"

        data = _build_test_epub(
            guide=[{"href": "cover.xhtml", "title": "Cover", "type": "cover"}],
            toc=(
                epub.Link("chap1.xhtml", "Chapter One", "c1"),
                epub.Link("chap2.xhtml", "Chapter Two", "c2"),
            ),
            spine_items=[cover, c1, c2],
        )

        chapters = extract_chapters(data)

        self.assertEqual([c.order for c in chapters], [1, 2])
        self.assertEqual([c.title for c in chapters], ["Chapter One", "Chapter Two"])
        self.assertIn("First chapter body.", chapters[0].content_html)
        self.assertIn("Second chapter body.", chapters[1].content_html)

    def test_well_formed_epub2_ncx_style_still_parses_via_toc(self):
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = "<html><body><p>Only chapter.</p></body></html>"

        data = _build_test_epub(
            toc=(epub.Link("chap1.xhtml", "NCX Chapter Title", "c1"),),
            spine_items=[c1],
        )

        chapters = extract_chapters(data)

        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].title, "NCX Chapter Title")

    def test_title_falls_back_to_heading_then_generated_placeholder(self):
        with_heading = epub.EpubHtml(title="a", file_name="a.xhtml", lang="en")
        with_heading.content = "<html><body><h2>Heading Title</h2><p>text</p></body></html>"
        without_heading = epub.EpubHtml(title="b", file_name="b.xhtml", lang="en")
        without_heading.content = "<html><body><p>no heading at all</p></body></html>"

        data = _build_test_epub(toc=(), spine_items=[with_heading, without_heading])

        chapters = extract_chapters(data)

        self.assertEqual(chapters[0].title, "Heading Title")
        self.assertEqual(chapters[1].title, "Chapter 2")

    def test_cover_and_nav_items_are_excluded_from_output(self):
        cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml", lang="en")
        cover.content = "<html><body><h1>Cover</h1></body></html>"
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = "<html><body><p>Real chapter.</p></body></html>"

        data = _build_test_epub(
            guide=[{"href": "cover.xhtml", "title": "Cover", "type": "cover"}],
            toc=(epub.Link("chap1.xhtml", "Real Chapter", "c1"),),
            spine_items=[cover, c1],
        )

        chapters = extract_chapters(data)

        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].title, "Real Chapter")

    def test_images_are_stripped_and_internal_links_unwrapped(self):
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = (
            "<html><body><p>Hello <img src='pic.jpg'/> world "
            "<a href='chap2.xhtml'>internal link text</a> and "
            "<a href='https://example.com'>external link</a>.</p></body></html>"
        )

        data = _build_test_epub(toc=(), spine_items=[c1])

        chapters = extract_chapters(data)

        self.assertNotIn("<img", chapters[0].content_html)
        self.assertNotIn("chap2", chapters[0].content_html)
        self.assertIn("internal link text", chapters[0].content_html)
        self.assertIn('href="https://example.com"', chapters[0].content_html)

    def test_non_ascii_smart_quotes_and_dashes_are_not_mangled(self):
        # Regression: get_body_content() returns undecoded UTF-8 bytes with
        # no encoding declaration of its own (that lives in the full
        # document's <head>, absent from a body-only fragment). Parsing
        # those bytes directly let libxml2 guess the encoding — defaulting
        # to a Latin-1-style byte-for-char mapping that mangled every
        # multi-byte character into mojibake (e.g. a UTF-8 curly quote
        # became a stray "â").
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = (
            "<html><body><p>“I incline to Cain’s heresy,” he said "
            "— quaintly.</p></body></html>"
        )
        data = _build_test_epub(toc=(epub.Link("chap1.xhtml", "Chapter", "c1"),), spine_items=[c1])

        chapters = extract_chapters(data)

        self.assertIn("“I incline to Cain’s heresy,”", chapters[0].content_html)
        self.assertIn("—", chapters[0].content_html)
        self.assertNotIn("â", chapters[0].content_html)

    def test_malformed_non_epub_bytes_raise_epub_parse_error(self):
        with self.assertRaises(EpubParseError):
            extract_chapters(b"this is not a zip file at all")

    def test_valid_zip_but_missing_ocf_mimetype_entry_raises(self):
        buf = BytesIO()
        import zipfile

        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("hello.txt", "not an epub")

        with self.assertRaises(EpubParseError):
            extract_chapters(buf.getvalue())


class RunEpubImportTests(TransactionTestCase):
    """Uses TransactionTestCase (not TestCase) because run_epub_import calls
    transaction.atomic() and connections.close_all() itself — behavior that
    TestCase's wrap-everything-in-one-rolled-back-transaction would mask."""

    # TransactionTestCase.tearDown() truncates all tables (flush), which
    # would also wipe the story_type migration's seeded StoryType rows for
    # every test that runs after this class — serialized_rollback restores
    # that initial data after each flush.
    serialized_rollback = True

    def _make_story(self):
        return Story.objects.create(title="Story", slug="story", epub_file="story_files/epubs/book.epub")

    def _run_import_with_epub_bytes(self, job_id, epub_bytes):
        # run_epub_import re-fetches its own Story instance from the DB (by
        # design — it runs in a worker thread with its own connection), so
        # instance-level monkeypatching on a test's local `story` object
        # never reaches it. Patch FieldFile.open at the class level instead,
        # which intercepts the call regardless of which instance opens it.
        from django.db.models.fields.files import FieldFile

        mock_file = MagicMock(__enter__=MagicMock(return_value=BytesIO(epub_bytes)), __exit__=MagicMock(return_value=False))
        with patch.object(FieldFile, "open", return_value=mock_file):
            run_epub_import(job_id)

    def test_success_path_creates_chapters_and_marks_job_completed(self):
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = "<html><body><p>Chapter body.</p></body></html>"
        epub_bytes = _build_test_epub(
            toc=(epub.Link("chap1.xhtml", "Only Chapter", "c1"),), spine_items=[c1]
        )
        story = self._make_story()
        job = EpubImportJob.objects.create(story=story)

        self._run_import_with_epub_bytes(job.id, epub_bytes)

        job.refresh_from_db()
        self.assertEqual(job.status, EpubImportJob.STATUS_COMPLETED)
        self.assertEqual(job.chapters_created, 1)
        self.assertEqual(story.chapters.count(), 1)
        self.assertEqual(story.chapters.first().title, "Only Chapter")

    def test_parse_failure_marks_job_failed_with_message_and_creates_no_chapters(self):
        story = self._make_story()
        job = EpubImportJob.objects.create(story=story)

        self._run_import_with_epub_bytes(job.id, b"not a real epub")

        job.refresh_from_db()
        self.assertEqual(job.status, EpubImportJob.STATUS_FAILED)
        self.assertTrue(job.error_message)
        self.assertEqual(story.chapters.count(), 0)

    def test_reimport_replaces_rather_than_duplicates_chapters(self):
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = "<html><body><p>Body.</p></body></html>"
        epub_bytes = _build_test_epub(
            toc=(epub.Link("chap1.xhtml", "Chapter", "c1"),), spine_items=[c1]
        )
        story = self._make_story()
        Chapter.objects.create(story=story, title="Hand-written", slug="hand-written", content="x", order=1)

        job = EpubImportJob.objects.create(story=story)
        self._run_import_with_epub_bytes(job.id, epub_bytes)

        self.assertEqual(story.chapters.count(), 1)
        self.assertEqual(story.chapters.first().title, "Chapter")


class ImportEpubApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin@example.com", username="admin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_import_epub_returns_202_immediately_without_waiting_on_processing(self):
        story = Story.objects.create(title="Story", slug="story", epub_file="story_files/epubs/book.epub")
        self.client.force_authenticate(user=self._make_superuser())

        # APITestCase wraps each test in its own transaction that's rolled
        # back afterwards, so transaction.on_commit callbacks never fire
        # under a plain request — captureOnCommitCallbacks(execute=True) is
        # Django's own helper for exercising them in tests anyway.
        with patch("apps.story.api.epub_import_executor") as mock_executor:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/admin/stories/{story.id}/import-epub/")

        self.assertEqual(response.status_code, 202)
        self.assertIn("id", response.data)
        self.assertEqual(response.data["status"], EpubImportJob.STATUS_PENDING)
        mock_executor.submit.assert_called_once()
        self.assertEqual(EpubImportJob.objects.filter(story=story).count(), 1)

    def test_import_epub_requires_story_to_have_an_epub_file(self):
        story = Story.objects.create(title="Story", slug="story")
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.post(f"/api/admin/stories/{story.id}/import-epub/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(EpubImportJob.objects.filter(story=story).count(), 0)

    def test_import_epub_accepts_a_direct_upload_when_story_has_no_epub_file(self):
        story = Story.objects.create(title="Story", slug="story")
        self.client.force_authenticate(user=self._make_superuser())
        c1 = epub.EpubHtml(title="c1", file_name="chap1.xhtml", lang="en")
        c1.content = "<html><body><p>Body.</p></body></html>"
        epub_bytes = _build_test_epub(toc=(epub.Link("chap1.xhtml", "Chapter", "c1"),), spine_items=[c1])
        uploaded = SimpleUploadedFile("book.epub", epub_bytes, content_type="application/epub+zip")

        # Story.epub_file uses the real (S3/R2-backed) DEFAULT_FILE_STORAGE —
        # patch the storage save/exists calls so this stays a hermetic test,
        # same reasoning as RunEpubImportTests patching FieldFile.open.
        from django.core.files.storage import default_storage

        with patch.object(default_storage, "save", return_value="story_files/epubs/book.epub"), patch.object(
            default_storage, "exists", return_value=False
        ):
            response = self.client.post(
                f"/api/admin/stories/{story.id}/import-epub/", {"epub_file": uploaded}, format="multipart"
            )

        self.assertEqual(response.status_code, 202)
        story.refresh_from_db()
        self.assertTrue(story.epub_file)
        self.assertEqual(EpubImportJob.objects.filter(story=story).count(), 1)

    def test_import_epub_rejects_a_direct_upload_with_the_wrong_extension(self):
        story = Story.objects.create(title="Story", slug="story")
        self.client.force_authenticate(user=self._make_superuser())
        disguised = SimpleUploadedFile("not-epub.exe", b"MZ fake executable bytes", content_type="application/octet-stream")

        response = self.client.post(
            f"/api/admin/stories/{story.id}/import-epub/", {"epub_file": disguised}, format="multipart"
        )

        self.assertEqual(response.status_code, 400)
        story.refresh_from_db()
        self.assertFalse(story.epub_file)
        self.assertEqual(EpubImportJob.objects.filter(story=story).count(), 0)

    def test_import_epub_is_gated_to_superusers(self):
        story = Story.objects.create(title="Story", slug="story", epub_file="story_files/epubs/book.epub")
        regular_user = User.objects.create(
            email="user@example.com", username="user", is_superuser=False, is_active=True
        )
        self.client.force_authenticate(user=regular_user)

        response = self.client.post(f"/api/admin/stories/{story.id}/import-epub/")

        self.assertEqual(response.status_code, 403)

    def test_import_epub_status_returns_current_job_state(self):
        story = Story.objects.create(title="Story", slug="story", epub_file="story_files/epubs/book.epub")
        job = EpubImportJob.objects.create(
            story=story, status=EpubImportJob.STATUS_COMPLETED, chapters_created=3
        )
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.get(f"/api/admin/stories/{story.id}/import-epub/{job.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], EpubImportJob.STATUS_COMPLETED)
        self.assertEqual(response.data["chapters_created"], 3)

    def test_import_epub_status_404s_for_unknown_job(self):
        story = Story.objects.create(title="Story", slug="story", epub_file="story_files/epubs/book.epub")
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.get(f"/api/admin/stories/{story.id}/import-epub/999999/")

        self.assertEqual(response.status_code, 404)


class DeleteChapterReordersRemainingChaptersTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin2@example.com", username="admin2", is_superuser=True, is_staff=True, is_active=True
        )

    def _make_story_with_chapters(self, count):
        story = Story.objects.create(title="Story", slug="story")
        chapters = [
            Chapter.objects.create(story=story, title=f"Chapter {n}", slug=f"chapter-{n}", content="x", order=n)
            for n in range(1, count + 1)
        ]
        return story, chapters

    def test_deleting_a_middle_chapter_shifts_later_ones_down_and_leaves_earlier_ones_alone(self):
        story, chapters = self._make_story_with_chapters(4)
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.delete(f"/api/admin/chapters/{chapters[3].id}/")  # order=4

        self.assertEqual(response.status_code, 204)
        remaining = list(Chapter.objects.filter(story=story).order_by("order"))
        self.assertEqual([c.order for c in remaining], [1, 2, 3])
        self.assertEqual([c.title for c in remaining], ["Chapter 1", "Chapter 2", "Chapter 3"])

    def test_deleting_the_first_chapter_shifts_everything_else_down(self):
        story, chapters = self._make_story_with_chapters(4)
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.delete(f"/api/admin/chapters/{chapters[0].id}/")  # order=1

        self.assertEqual(response.status_code, 204)
        remaining = list(Chapter.objects.filter(story=story).order_by("order"))
        self.assertEqual([c.order for c in remaining], [1, 2, 3])
        self.assertEqual([c.title for c in remaining], ["Chapter 2", "Chapter 3", "Chapter 4"])

    def test_deleting_the_last_chapter_leaves_the_rest_untouched(self):
        story, chapters = self._make_story_with_chapters(3)
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.delete(f"/api/admin/chapters/{chapters[2].id}/")  # order=3

        self.assertEqual(response.status_code, 204)
        remaining = list(Chapter.objects.filter(story=story).order_by("order"))
        self.assertEqual([c.order for c in remaining], [1, 2])
        self.assertEqual([c.title for c in remaining], ["Chapter 1", "Chapter 2"])


class StoryExternalLinkTests(APITestCase):
    def test_admin_saves_external_link_fields_and_story_detail_returns_them(self):
        self.client.force_authenticate(
            user=User.objects.create(
                email="linkadmin@example.com", username="linkadmin", is_superuser=True, is_staff=True, is_active=True
            )
        )
        story = Story.objects.create(title="Linked Story", slug="linked-story", is_published=True)

        response = self.client.patch(
            f"/api/admin/stories/{story.id}/",
            {
                "external_link": "https://www.amazon.com/dp/B000000000",
                "link_note": "Available on Amazon",
                "link_label": "Buy on Amazon",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("story-detail", args=[story.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["external_link"], "https://www.amazon.com/dp/B000000000")
        self.assertEqual(response.data["link_note"], "Available on Amazon")
        self.assertEqual(response.data["link_label"], "Buy on Amazon")

    def test_external_link_fields_default_to_empty(self):
        story = Story.objects.create(title="Plain Story", slug="plain-story", is_published=True)

        response = self.client.get(reverse("story-detail", args=[story.slug]))

        self.assertEqual(response.data["external_link"], "")
        self.assertEqual(response.data["link_note"], "")
        self.assertEqual(response.data["link_label"], "")


class VideoAdminApiTests(APITestCase):
    URL = "/api/admin/videos/"
    YOUTUBE_URL = "https://youtu.be/dQw4w9WgXcQ"

    def setUp(self):
        self.client.force_authenticate(
            user=User.objects.create(
                email="videoadmin@example.com", username="videoadmin", is_superuser=True, is_staff=True, is_active=True
            )
        )

    def _video(self, story=None, **kwargs):
        defaults = {"title": "Clip", "slug": "clip", "order": 1, "youtube_url": self.YOUTUBE_URL, "youtube_id": "dQw4w9WgXcQ"}
        defaults.update(kwargs)
        return Video.objects.create(story=story, **defaults)

    def test_create_standalone_video(self):
        response = self.client.post(self.URL, {"title": "Standalone Clip", "youtube_url": self.YOUTUBE_URL}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["story"])
        self.assertIsNone(response.data["story_detail"])
        self.assertEqual(response.data["slug"], "standalone-clip")
        self.assertEqual(response.data["youtube_id"], "dQw4w9WgXcQ")

    def test_create_for_story_without_order_appends_to_end(self):
        story = Story.objects.create(title="Story", slug="story")
        self._video(story=story, order=1)
        self._video(story=story, slug="clip-2", order=2)

        response = self.client.post(
            self.URL, {"story": story.id, "title": "Third", "youtube_url": self.YOUTUBE_URL}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["order"], 3)
        self.assertEqual(response.data["story_detail"]["slug"], "story")

    def test_detaching_video_from_story(self):
        story = Story.objects.create(title="Story", slug="story")
        video = self._video(story=story)

        response = self.client.patch(f"{self.URL}{video.id}/", {"story": None}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        video.refresh_from_db()
        self.assertIsNone(video.story)

    def test_list_is_paged_newest_first_and_filters_by_has_story(self):
        story = Story.objects.create(title="Story", slug="story")
        older = self._video(story=story, slug="older")
        newer = self._video(slug="newer")
        Video.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=1))

        response = self.client.get(self.URL)
        self.assertEqual([v["id"] for v in response.data["results"]], [newer.id, older.id])
        self.assertIn("pages", response.data["pagination"])

        response = self.client.get(self.URL, {"has_story": "false"})
        self.assertEqual([v["id"] for v in response.data["results"]], [newer.id])

    def test_deleting_standalone_video(self):
        video = self._video()

        response = self.client.delete(f"{self.URL}{video.id}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Video.objects.filter(pk=video.pk).exists())


class WatchCatalogueApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.wide = VideoSeries.objects.create(name="Wide", slug="wide-test", order=1, layout="wide")
        self.tall = VideoSeries.objects.create(name="Tall", slug="tall-test", order=2, layout="tall")
        self.story = Story.objects.create(title="Shared Story", slug="shared-story", is_published=True)

    def video(self, title, duration, series=None, story=None, order=1, **kwargs):
        return Video.objects.create(
            title=title,
            display_title=kwargs.pop("display_title", title),
            slug=kwargs.pop("slug", title.lower().replace(" ", "-")),
            youtube_id=kwargs.pop("youtube_id", f"id{Video.objects.count():09d}"),
            youtube_url="https://youtu.be/dQw4w9WgXcQ",
            duration_seconds=duration,
            series=series or self.wide,
            story=story,
            order=order,
            **kwargs,
        )

    def ids(self, response):
        return [row["id"] for row in response.data["results"]]

    def test_series_length_search_and_duration_null_behaviour(self):
        short = self.video("Short", 299, self.tall)
        mid = self.video("Needle Display", 300, self.wide, display_title="Needle Display")
        long = self.video("Long", 1200, self.wide)
        unknown = self.video("Unknown", None, self.wide)

        self.assertEqual(self.ids(self.client.get("/api/videos/", {"series": self.tall.slug})), [short.id])
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"length": "short"})), [short.id])
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"length": "mid"})), [mid.id])
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"length": "long"})), [long.id])
        self.assertIn(unknown.id, self.ids(self.client.get("/api/videos/")))
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"q": "needle"})), [mid.id])

    def test_shortest_and_longest_sort(self):
        short = self.video("Short", 60)
        long = self.video("Long", 600)
        self.video("Unknown", None)
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"sort": "shortest"})), [short.id, long.id])
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"sort": "longest"})), [long.id, short.id])

    def test_most_watched_uses_audience_events_and_excludes_staff_and_bots(self):
        popular = self.video("Popular", 60)
        noise = self.video("Noise", 60)
        staff = User.objects.create(email="staff-watch@example.com", username="staff-watch", is_staff=True)
        AnalyticsEvent.objects.create(event_type="watching_session", visitor_id="real-1", video=popular)
        AnalyticsEvent.objects.create(event_type="watching_session", visitor_id="real-2", video=popular)
        AnalyticsEvent.objects.create(event_type="watching_session", visitor_id="bot", video=noise, is_suspected_bot=True)
        AnalyticsEvent.objects.create(event_type="watching_session", visitor_id="staff", video=noise, user=staff)
        self.assertEqual(self.ids(self.client.get("/api/videos/", {"sort": "most_watched"}))[:2], [popular.id, noise.id])

    def test_other_versions_excludes_the_video_itself(self):
        first = self.video("First", 60, story=self.story, order=1)
        second = self.video("Second", 120, story=self.story, order=2)
        response = self.client.get(f"/api/videos/{first.id}/")
        self.assertEqual([row["id"] for row in response.data["other_versions"]], [second.id])

    def test_overview_featured_rank_and_fallback(self):
        older = self.video("Older", 60, featured_rank=2)
        chosen = self.video("Chosen", 60, featured_rank=1)
        response = self.client.get("/api/videos/overview/")
        self.assertEqual(response.data["featured"]["id"], chosen.id)
        Video.objects.update(featured_rank=None)
        cache.clear()
        response = self.client.get("/api/videos/overview/")
        self.assertEqual(response.data["featured"]["id"], chosen.id)
        self.assertNotEqual(older.id, chosen.id)

    def test_continue_watching_is_only_returned_to_authenticated_user(self):
        video = self.video("Continue", 100)
        user = User.objects.create(email="watcher@example.com", username="watcher", is_active=True)
        VideoWatchProgress.objects.create(user=user, video=video, progress=.4, position_seconds=40, duration_seconds=100)
        self.assertEqual(self.client.get("/api/videos/overview/").data["continue_watching"], [])
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/videos/overview/")
        self.assertEqual(response.data["continue_watching"][0]["video"]["id"], video.id)
        self.assertEqual(response.data["continue_watching"][0]["seconds_left"], 60)


def _mock_response(text="Generated text.", confident=True, note=""):
    resp = MagicMock()
    resp.parsed_output = _GenerationOutput(text=text, confident=confident, confidence_note=note)
    return resp


class GenerateFieldPromptLogicTests(SimpleTestCase):
    """ai_generation.py is pure (no DB) — exercises the retry/fallback state
    machine against a mocked anthropic.Anthropic client."""

    def test_metadata_mode_single_success_makes_one_call(self):
        with patch("apps.story.ai_generation._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_response()
            result = generate("summary", "instructions", "Title", "Author", ["title", "author"], None)

        self.assertEqual(result.source, "metadata")
        self.assertEqual(mock_client.return_value.messages.parse.call_count, 1)

    def test_content_mode_single_success_makes_one_call(self):
        with patch("apps.story.ai_generation._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_response()
            result = generate(
                "summary", "instructions", "Title", "Author", ["title", "author", "content"], "chapter text"
            )

        self.assertEqual(result.source, "content")
        self.assertEqual(mock_client.return_value.messages.parse.call_count, 1)

    def test_low_confidence_is_returned_as_is_without_an_extra_retry_call(self):
        # Regression guard for the explicit product decision: a
        # confident=False result must never trigger an automatic retry — the
        # admin decides whether to retry, not the backend.
        with patch("apps.story.ai_generation._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_response(
                confident=False, note="Not familiar with this specific edition."
            )
            result = generate("summary", "instructions", "Title", "Author", ["title", "author"], None)

        self.assertFalse(result.confident)
        self.assertEqual(result.confidence_note, "Not familiar with this specific edition.")
        self.assertEqual(mock_client.return_value.messages.parse.call_count, 1)

    def test_hard_failure_then_retry_success_makes_two_calls_no_fallback(self):
        with patch("apps.story.ai_generation._client") as mock_client:
            mock_client.return_value.messages.parse.side_effect = [
                anthropic.APIConnectionError(request=MagicMock()),
                _mock_response(),
            ]
            result = generate("summary", "instructions", "Title", "Author", ["title", "author"], None)

        self.assertEqual(result.source, "metadata")
        self.assertEqual(mock_client.return_value.messages.parse.call_count, 2)

    def test_metadata_fails_twice_then_falls_back_to_content_and_succeeds(self):
        with patch("apps.story.ai_generation._client") as mock_client:
            err = anthropic.APIConnectionError(request=MagicMock())
            mock_client.return_value.messages.parse.side_effect = [err, err, _mock_response()]
            result = generate(
                "summary", "instructions", "Title", "Author", ["title", "author"], "chapter text"
            )

        self.assertEqual(result.source, "content")
        self.assertEqual(mock_client.return_value.messages.parse.call_count, 3)

    def test_metadata_fails_twice_and_fallback_also_fails_raises(self):
        with patch("apps.story.ai_generation._client") as mock_client:
            err = anthropic.APIConnectionError(request=MagicMock())
            mock_client.return_value.messages.parse.side_effect = [err, err, err]
            with self.assertRaises(GenerationError):
                generate("summary", "instructions", "Title", "Author", ["title", "author"], "chapter text")

        self.assertEqual(mock_client.return_value.messages.parse.call_count, 3)

    def test_content_mode_failure_twice_raises_after_exactly_two_calls(self):
        # No fallback from content mode — there's nothing further to fall
        # back to.
        with patch("apps.story.ai_generation._client") as mock_client:
            err = anthropic.APIConnectionError(request=MagicMock())
            mock_client.return_value.messages.parse.side_effect = [err, err]
            with self.assertRaises(GenerationError):
                generate(
                    "summary", "instructions", "Title", "Author", ["title", "author", "content"], "chapter text"
                )

        self.assertEqual(mock_client.return_value.messages.parse.call_count, 2)

    def test_empty_response_text_is_treated_as_a_failure(self):
        with patch("apps.story.ai_generation._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_response(text="   ")
            with self.assertRaises(GenerationError):
                generate("summary", "instructions", "Title", "Author", ["title", "author"], None)


class GenerationHtmlSanitizationTests(SimpleTestCase):
    def test_markdown_formatting_is_converted_to_rich_text_html(self):
        from apps.story.ai_generation import _to_html

        html = _to_html(
            "# A Heading\n\nThis is **bold** and *italic* text.\n\n"
            "- First point\n- Second point\n\n> A quoted line."
        )

        self.assertIn("<h1>A Heading</h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<li>First point</li>", html)
        self.assertIn("<blockquote>", html)

    def test_injected_markup_is_neutralized_despite_markdown_passthrough(self):
        from apps.story.ai_generation import _to_html

        # Markdown passes raw HTML blocks through unchanged by design — nh3
        # sanitization afterward is the real security boundary here, not the
        # markdown conversion step.
        html = _to_html("Normal text.\n\n<script>alert('xss')</script>\n\n[click](javascript:alert(1))")

        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:", html)


class RunGenerateFieldTests(TransactionTestCase):
    """Uses TransactionTestCase (not TestCase) for the same reason as
    RunEpubImportTests — this touches real DB connections from what's
    effectively a standalone function call, mirroring how it runs from a
    worker thread in production."""

    serialized_rollback = True  # see RunEpubImportTests for why

    def _make_story(self, with_chapter=True):
        story = Story.objects.create(title="Story", slug="story")
        if with_chapter:
            Chapter.objects.create(story=story, title="Ch1", slug="ch1", content="<p>Some chapter text.</p>", order=1)
        return story

    def test_success_path_updates_only_the_targeted_actions_columns(self):
        story = self._make_story()
        with patch("apps.story.ai_generation_jobs.generate") as mock_generate:
            mock_generate.return_value = MagicMock(
                content="<p>Generated summary.</p>", source="content", confident=True, confidence_note=""
            )
            run_generate_field(story.id, "summary", ["title", "author", "content"])

        story.refresh_from_db()
        self.assertEqual(story.summary, "<p>Generated summary.</p>")
        self.assertEqual(story.summary_status, Story.GEN_STATUS_COMPLETED)
        self.assertEqual(story.summary_source, "content")
        self.assertTrue(story.summary_confident)
        self.assertIsNone(story.summary_error)
        # Retrospective columns untouched
        self.assertIsNone(story.retrospective_status)
        self.assertIsNone(story.retrospective)

    def test_uses_the_admin_selected_model_per_action(self):
        story = self._make_story()
        prompt_settings = PromptSettings.get_solo()
        prompt_settings.summary_model = "claude-haiku-4-5"
        prompt_settings.retrospective_model = "claude-opus-5"
        prompt_settings.save()

        with patch("apps.story.ai_generation_jobs.generate") as mock_generate:
            mock_generate.return_value = MagicMock(content="<p>S.</p>", source="content", confident=True, confidence_note="")
            run_generate_field(story.id, "summary", ["title", "author", "content"])
            mock_generate.return_value = MagicMock(content="<p>R.</p>", source="content", confident=True, confidence_note="")
            run_generate_field(story.id, "retrospective", ["title", "author", "content"])

        summary_call_kwargs = mock_generate.call_args_list[0].kwargs
        retrospective_call_kwargs = mock_generate.call_args_list[1].kwargs
        self.assertEqual(summary_call_kwargs["model"], "claude-haiku-4-5")
        self.assertEqual(retrospective_call_kwargs["model"], "claude-opus-5")

    def test_failure_path_marks_failed_and_leaves_text_field_untouched(self):
        story = self._make_story()
        story.summary = "<p>Pre-existing hand-written summary.</p>"
        story.save(update_fields=["summary"])

        with patch("apps.story.ai_generation_jobs.generate", side_effect=GenerationError("boom")):
            run_generate_field(story.id, "summary", ["title", "author"])

        story.refresh_from_db()
        self.assertEqual(story.summary_status, Story.GEN_STATUS_FAILED)
        self.assertEqual(story.summary_error, "boom")
        self.assertEqual(story.summary, "<p>Pre-existing hand-written summary.</p>")

    def test_concatenated_chapter_text_follows_order_not_insertion_order(self):
        story = Story.objects.create(title="Story", slug="story")
        Chapter.objects.create(story=story, title="Second", slug="second", content="<p>second text</p>", order=2)
        Chapter.objects.create(story=story, title="First", slug="first", content="<p>first text</p>", order=1)

        text = _concatenated_chapter_text(story)

        self.assertLess(text.index("first text"), text.index("second text"))
        self.assertNotIn("<p>", text)

    def test_concatenated_chapter_text_truncates_at_cap(self):
        from apps.story.ai_generation import MAX_CONTENT_CHARS

        story = Story.objects.create(title="Story", slug="story")
        Chapter.objects.create(story=story, title="Long", slug="long", content="x" * (MAX_CONTENT_CHARS + 5000), order=1)

        text = _concatenated_chapter_text(story)

        self.assertLessEqual(len(text), MAX_CONTENT_CHARS)

    def test_concurrent_summary_and_retrospective_runs_dont_clobber_each_other(self):
        story = self._make_story()
        with patch("apps.story.ai_generation_jobs.generate") as mock_generate:
            mock_generate.return_value = MagicMock(content="<p>Summary.</p>", source="content", confident=True, confidence_note="")
            run_generate_field(story.id, "summary", ["title", "author", "content"])

            mock_generate.return_value = MagicMock(content="<p>Retrospective.</p>", source="metadata", confident=False, confidence_note="unsure")
            run_generate_field(story.id, "retrospective", ["title", "author"])

        story.refresh_from_db()
        self.assertEqual(story.summary, "<p>Summary.</p>")
        self.assertEqual(story.summary_status, Story.GEN_STATUS_COMPLETED)
        self.assertEqual(story.summary_source, "content")
        self.assertEqual(story.retrospective, "<p>Retrospective.</p>")
        self.assertEqual(story.retrospective_status, Story.GEN_STATUS_COMPLETED)
        self.assertEqual(story.retrospective_source, "metadata")
        self.assertFalse(story.retrospective_confident)


class GenerateSummaryRetrospectiveApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin3@example.com", username="admin3", is_superuser=True, is_staff=True, is_active=True
        )

    def _make_story(self, with_chapter=True):
        story = Story.objects.create(title="Story", slug="story")
        if with_chapter:
            Chapter.objects.create(story=story, title="Ch1", slug="ch1", content="text", order=1)
        return story

    def test_generate_summary_returns_202_immediately_without_waiting_on_processing(self):
        story = self._make_story()
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.ai_generation_executor") as mock_executor:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/admin/stories/{story.id}/generate-summary/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["summary_status"], Story.GEN_STATUS_PENDING)
        mock_executor.submit.assert_called_once()
        _, called_story_id, called_action, called_fields = mock_executor.submit.call_args.args
        self.assertEqual(called_story_id, story.id)
        self.assertEqual(called_action, "summary")
        self.assertEqual(called_fields, ["title", "author", "content"])

    def test_omitted_input_fields_defaults_to_content_mode(self):
        story = self._make_story()
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.ai_generation_executor") as mock_executor:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(f"/api/admin/stories/{story.id}/generate-summary/", {}, format="json")

        _, _, _, called_fields = mock_executor.submit.call_args.args
        self.assertEqual(called_fields, ["title", "author", "content"])

    def test_explicit_metadata_mode_accepted(self):
        story = self._make_story()
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.ai_generation_executor"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    f"/api/admin/stories/{story.id}/generate-summary/",
                    {"input_fields": ["title", "author"]},
                    format="json",
                )

        self.assertEqual(response.status_code, 202)

    def test_invalid_input_fields_rejected(self):
        story = self._make_story()
        self.client.force_authenticate(user=self._make_superuser())

        for bad_value in (
            ["content"],
            [],
            ["title"],
            ["title", "author", "content", "extra"],
            ["title", "title", "author"],
            "not-a-list",
        ):
            response = self.client.post(
                f"/api/admin/stories/{story.id}/generate-summary/", {"input_fields": bad_value}, format="json"
            )
            self.assertEqual(response.status_code, 400, f"expected 400 for {bad_value!r}")
        self.assertIsNone(Story.objects.get(pk=story.id).summary_status)

    def test_content_mode_rejected_when_story_has_no_chapters(self):
        story = self._make_story(with_chapter=False)
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.post(
            f"/api/admin/stories/{story.id}/generate-summary/",
            {"input_fields": ["title", "author", "content"]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_gated_to_superusers(self):
        story = self._make_story()
        regular_user = User.objects.create(
            email="regular2@example.com", username="regular2", is_superuser=False, is_active=True
        )
        self.client.force_authenticate(user=regular_user)

        response = self.client.post(f"/api/admin/stories/{story.id}/generate-summary/")

        self.assertEqual(response.status_code, 403)

    def test_generate_retrospective_is_wired_to_its_own_fields_not_summarys(self):
        story = self._make_story()
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.ai_generation_executor") as mock_executor:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/admin/stories/{story.id}/generate-retrospective/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["retrospective_status"], Story.GEN_STATUS_PENDING)
        self.assertIsNone(response.data["summary_status"])
        _, _, called_action, _ = mock_executor.submit.call_args.args
        self.assertEqual(called_action, "retrospective")


class PromptSettingsApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin4@example.com", username="admin4", is_superuser=True, is_staff=True, is_active=True
        )

    def test_get_returns_defaults_on_first_access(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.get("/api/admin/prompt-settings/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["summary_instructions"])
        self.assertEqual(response.data["summary_model"], "claude-sonnet-5")
        self.assertTrue(response.data["retrospective_instructions"])
        self.assertEqual(response.data["retrospective_model"], "claude-sonnet-5")

    def test_patch_updates_and_persists(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.patch(
            "/api/admin/prompt-settings/",
            {"summary_instructions": "Custom summary instructions.", "summary_model": "claude-opus-5"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary_instructions"], "Custom summary instructions.")
        self.assertEqual(response.data["summary_model"], "claude-opus-5")

        # Persisted to the same singleton row read elsewhere (get_solo()).
        self.assertEqual(PromptSettings.get_solo().summary_instructions, "Custom summary instructions.")

    def test_patch_updating_summary_leaves_retrospective_untouched(self):
        self.client.force_authenticate(user=self._make_superuser())
        PromptSettings.get_solo()  # ensure the row exists first

        self.client.patch("/api/admin/prompt-settings/", {"summary_model": "claude-haiku-4-5"}, format="json")

        self.assertEqual(PromptSettings.get_solo().retrospective_model, "claude-sonnet-5")

    def test_invalid_model_choice_rejected(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.patch(
            "/api/admin/prompt-settings/", {"summary_model": "gpt-4"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_gated_to_superusers(self):
        regular_user = User.objects.create(
            email="regular3@example.com", username="regular3", is_superuser=False, is_active=True
        )
        self.client.force_authenticate(user=regular_user)

        response = self.client.get("/api/admin/prompt-settings/")

        self.assertEqual(response.status_code, 403)


class BlogExcerptPlainTextTests(SimpleTestCase):
    def test_strips_markdown_and_unescapes_entities(self):
        result = _to_plain_text("A great **story** about dragons & magic.")

        self.assertEqual(result, "A great story about dragons & magic.")

    def test_truncates_at_word_boundary_with_ellipsis(self):
        result = _to_plain_text("word " * 100)

        self.assertLessEqual(len(result), 300)
        self.assertTrue(result.endswith("…"))
        self.assertNotIn("  ", result)

    def test_strips_surrounding_quotes(self):
        result = _to_plain_text('"Quoted excerpt here"')

        self.assertEqual(result, "Quoted excerpt here")


class RunGenerateBlogExcerptTests(TransactionTestCase):
    serialized_rollback = True

    def _make_blog(self):
        return Blog.objects.create(
            title="A Blog Post", slug="a-blog-post", content="<p>Some post content.</p>", author_name="Jane"
        )

    def test_success_path_updates_excerpt_and_status(self):
        blog = self._make_blog()
        with patch("apps.story.ai_generation_jobs.generate") as mock_generate:
            mock_generate.return_value = MagicMock(
                content="A punchy SEO excerpt.", source="content", confident=True, confidence_note=""
            )
            run_generate_blog_excerpt(blog.id)

        blog.refresh_from_db()
        self.assertEqual(blog.excerpt, "A punchy SEO excerpt.")
        self.assertEqual(blog.excerpt_status, Blog.GEN_STATUS_COMPLETED)
        self.assertEqual(blog.excerpt_source, "content")
        self.assertTrue(blog.excerpt_confident)
        self.assertIsNone(blog.excerpt_error)

    def test_always_grounds_in_blog_content_and_renders_as_text(self):
        blog = self._make_blog()
        with patch("apps.story.ai_generation_jobs.generate") as mock_generate:
            mock_generate.return_value = MagicMock(
                content="Plain excerpt.", source="content", confident=True, confidence_note=""
            )
            run_generate_blog_excerpt(blog.id)

        call_kwargs = mock_generate.call_args.kwargs
        self.assertEqual(call_kwargs["render_as"], "text")
        self.assertEqual(call_kwargs["action"], "excerpt")
        self.assertEqual(call_kwargs["input_fields"], ["title", "author", "content"])
        self.assertIn("Some post content.", call_kwargs["content_text"])

    def test_failure_path_marks_failed_and_leaves_excerpt_untouched(self):
        blog = self._make_blog()
        blog.excerpt = "Hand-written excerpt."
        blog.save(update_fields=["excerpt"])

        with patch("apps.story.ai_generation_jobs.generate", side_effect=GenerationError("boom")):
            run_generate_blog_excerpt(blog.id)

        blog.refresh_from_db()
        self.assertEqual(blog.excerpt_status, Blog.GEN_STATUS_FAILED)
        self.assertEqual(blog.excerpt_error, "boom")
        self.assertEqual(blog.excerpt, "Hand-written excerpt.")

    def test_uses_prompt_settings_excerpt_model_and_instructions(self):
        blog = self._make_blog()
        prompt_settings = PromptSettings.get_solo()
        prompt_settings.excerpt_model = "claude-opus-5"
        prompt_settings.excerpt_instructions = "Custom excerpt instructions."
        prompt_settings.save()

        with patch("apps.story.ai_generation_jobs.generate") as mock_generate:
            mock_generate.return_value = MagicMock(content="x", source="content", confident=True, confidence_note="")
            run_generate_blog_excerpt(blog.id)

        call_kwargs = mock_generate.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "claude-opus-5")
        self.assertEqual(call_kwargs["instructions"], "Custom excerpt instructions.")


class GenerateBlogExcerptApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin5@example.com", username="admin5", is_superuser=True, is_staff=True, is_active=True
        )

    def _make_blog(self):
        return Blog.objects.create(title="A Blog Post", slug="a-blog-post", content="<p>x</p>")

    def test_returns_202_immediately_without_waiting_on_processing(self):
        blog = self._make_blog()
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.ai_generation_executor") as mock_executor:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/admin/blog/{blog.id}/generate-excerpt/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["excerpt_status"], Blog.GEN_STATUS_PENDING)
        mock_executor.submit.assert_called_once_with(run_generate_blog_excerpt, blog.id)

    def test_gated_to_superusers(self):
        blog = self._make_blog()
        regular_user = User.objects.create(
            email="regular4@example.com", username="regular4", is_superuser=False, is_active=True
        )
        self.client.force_authenticate(user=regular_user)

        response = self.client.post(f"/api/admin/blog/{blog.id}/generate-excerpt/")

        self.assertEqual(response.status_code, 403)


class ExcerptFromTextTests(SimpleTestCase):
    LONG_TEXT = (
        "Alice was beginning to get very tired of sitting by her sister on the "
        "bank, and of having nothing to do: once or twice she had peeped into "
        "the book her sister was reading, but it had no pictures or "
        "conversations in it, and what is the use of a book, thought Alice, "
        "without pictures or conversations."
    )

    def test_empty_text_returns_empty_string(self):
        self.assertEqual(_excerpt_from_text("", 0.5), "")

    def test_progress_zero_starts_at_beginning_with_no_leading_ellipsis(self):
        excerpt = _excerpt_from_text(self.LONG_TEXT, 0.0)

        self.assertTrue(excerpt.startswith("Alice was beginning"))
        self.assertFalse(excerpt.startswith("…"))

    def test_progress_midway_starts_mid_text_with_leading_ellipsis(self):
        excerpt = _excerpt_from_text(self.LONG_TEXT, 0.5)

        self.assertTrue(excerpt.startswith("…"))
        self.assertNotEqual(excerpt, _excerpt_from_text(self.LONG_TEXT, 0.0))

    def test_short_text_returns_whole_text_with_no_ellipses(self):
        short_text = "One two three four five."

        excerpt = _excerpt_from_text(short_text, 0.0)

        self.assertEqual(excerpt, "One two three four five.")

    def test_progress_at_end_falls_back_to_tail_words(self):
        excerpt = _excerpt_from_text(self.LONG_TEXT, 1.0)

        self.assertTrue(excerpt.startswith("…"))
        self.assertTrue(excerpt.endswith("conversations."))

    def test_snippet_reaching_end_of_text_has_no_trailing_ellipsis(self):
        words = " ".join(f"word{i}" for i in range(25))

        excerpt = _excerpt_from_text(words, 0.5, word_count=30)

        self.assertFalse(excerpt.endswith("…"))

    def test_snippet_not_reaching_end_of_text_has_trailing_ellipsis(self):
        words = " ".join(f"word{i}" for i in range(100))

        excerpt = _excerpt_from_text(words, 0.0, word_count=10)

        self.assertTrue(excerpt.endswith("…"))


def _book_record(**overrides):
    fields = dict(
        title="A Public Domain Book",
        author_name="Some Author",
        about="A short synopsis.",
        story_type="Novel",
        country="Japan",
        language="Japanese",
        genres=["Adventure"],
        categories=["Classic Literature"],
        original_published_year=1900,
        original_published_month=None,
        original_published_day=None,
        epub_link="",
        pdf_link="",
        cover_image_link="",
    )
    fields.update(overrides)
    return _BookRecord(**fields)


def _mock_book_fetch_response(records):
    resp = MagicMock()
    resp.parsed_output = _BookFetchOutput(books=records)
    return resp


class BookFetchPromptLogicTests(SimpleTestCase):
    """book_fetch.py is pure (no DB) — exercises the API-call/parse logic
    against a mocked anthropic.Anthropic client."""

    def test_successful_call_returns_the_parsed_books(self):
        with patch("apps.story.book_fetch._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_book_fetch_response(
                [_book_record(title="Book One"), _book_record(title="Book Two")]
            )
            books = fetch_books("title,author\n", 2, "instructions", ["Novel"])

        self.assertEqual([b.title for b in books], ["Book One", "Book Two"])
        mock_client.return_value.messages.parse.assert_called_once()
        call_kwargs = mock_client.return_value.messages.parse.call_args.kwargs
        self.assertEqual(call_kwargs["output_format"], _BookFetchOutput)

    def test_api_errors_all_map_to_book_fetch_error(self):
        for exc in (
            anthropic.NotFoundError(message="nf", response=MagicMock(), body=None),
            anthropic.RateLimitError(message="rl", response=MagicMock(), body=None),
            anthropic.APIConnectionError(request=MagicMock()),
        ):
            with patch("apps.story.book_fetch._client") as mock_client:
                mock_client.return_value.messages.parse.side_effect = exc
                with self.assertRaises(BookFetchError):
                    fetch_books("title,author\n", 5, "instructions", ["Novel"])

    def test_empty_books_list_raises_book_fetch_error(self):
        with patch("apps.story.book_fetch._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_book_fetch_response([])
            with self.assertRaises(BookFetchError):
                fetch_books("title,author\n", 5, "instructions", ["Novel"])

    def test_max_tokens_scales_with_count_but_stays_capped(self):
        self.assertEqual(_max_tokens_for(1), 4_500)
        self.assertEqual(_max_tokens_for(14), 20_000)
        self.assertEqual(_max_tokens_for(100), 20_000)

    def test_truncated_json_response_raises_book_fetch_error(self):
        with patch("apps.story.book_fetch._client") as mock_client:
            mock_client.return_value.messages.parse.side_effect = PydanticValidationError.from_exception_data(
                "_BookFetchOutput", []
            )
            with self.assertRaises(BookFetchError):
                fetch_books("title,author\n", 5, "instructions", ["Novel"])


class RunBookFetchTests(TransactionTestCase):
    """Uses TransactionTestCase (not TestCase) because run_book_fetch calls
    transaction.atomic() and connections.close_all() itself, same reasoning
    as RunEpubImportTests."""

    serialized_rollback = True  # see RunEpubImportTests for why

    def test_success_path_creates_queue_rows_and_marks_job_completed(self):
        job = BookFetchJob.objects.create(requested_count=2)
        records = [_book_record(title="Book One"), _book_record(title="Book Two")]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.status, BookFetchJob.STATUS_COMPLETED)
        self.assertEqual(job.created_count, 2)
        self.assertEqual(job.skipped_count, 0)
        self.assertEqual(StoryQueue.objects.count(), 2)
        created = StoryQueue.objects.get(title="Book One")
        self.assertEqual(created.country, "JP")
        self.assertEqual(created.language, "ja")
        self.assertEqual(created.story_type.name, "Novel")
        self.assertEqual(list(created.genres.values_list("name", flat=True)), ["Adventure"])
        self.assertEqual(list(created.categories.values_list("name", flat=True)), ["Classic Literature"])

    def test_skips_a_record_matching_an_existing_story_by_title_and_author(self):
        Story.objects.create(
            title="Already Here", slug="already-here", author=Author.objects.create(name="Some Author")
        )
        job = BookFetchJob.objects.create(requested_count=1)
        records = [_book_record(title="Already Here", author_name="Some Author")]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.created_count, 0)
        self.assertEqual(job.skipped_count, 1)
        self.assertEqual(StoryQueue.objects.count(), 0)

    def test_does_not_skip_same_title_different_author(self):
        # Dedup key is (title, author) together, not title alone — a
        # different author writing a same-titled book is not a duplicate.
        Story.objects.create(
            title="Emma", slug="emma-1", author=Author.objects.create(name="Jane Austen")
        )
        job = BookFetchJob.objects.create(requested_count=1)
        records = [_book_record(title="Emma", author_name="Someone Else")]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.created_count, 1)
        self.assertEqual(job.skipped_count, 0)

    def test_skips_a_record_matching_a_not_yet_added_queue_entry(self):
        StoryQueue.objects.create(title="In The Queue", author_name="Queue Author")
        job = BookFetchJob.objects.create(requested_count=1)
        records = [_book_record(title="In The Queue", author_name="Queue Author")]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.created_count, 0)
        self.assertEqual(job.skipped_count, 1)

    def test_within_batch_duplicate_keeps_only_the_first(self):
        job = BookFetchJob.objects.create(requested_count=2)
        records = [
            _book_record(title="Same Book", author_name="Same Author", about="First."),
            _book_record(title="same book", author_name="same author", about="Second."),
        ]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.created_count, 1)
        self.assertEqual(job.skipped_count, 1)
        self.assertEqual(StoryQueue.objects.get().about, "First.")

    def test_genre_and_category_names_resolve_case_insensitively_or_create(self):
        existing_genre = Genre.objects.create(name="Adventure")
        job = BookFetchJob.objects.create(requested_count=1)
        records = [_book_record(genres=["adventure", "Mystery"], categories=["New Category"])]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        created = StoryQueue.objects.get()
        self.assertEqual(list(created.genres.values_list("id", flat=True)), [existing_genre.id, Genre.objects.get(name="Mystery").id])
        self.assertEqual(Category.objects.filter(name="New Category").count(), 1)

    def test_invalid_story_type_country_and_language_save_as_blank(self):
        job = BookFetchJob.objects.create(requested_count=1)
        records = [
            _book_record(
                story_type="Not A Real Type", country="Not A Real Country", language="Not A Real Language"
            )
        ]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        created = StoryQueue.objects.get()
        self.assertIsNone(created.story_type)
        self.assertEqual(created.country, "")
        self.assertEqual(created.language, "")

    def test_invalid_published_date_parts_are_sanitized(self):
        job = BookFetchJob.objects.create(requested_count=1)
        records = [
            _book_record(original_published_year=1900, original_published_month=13, original_published_day=5)
        ]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        created = StoryQueue.objects.get()
        self.assertEqual(created.original_published_year, 1900)
        self.assertIsNone(created.original_published_month)
        self.assertIsNone(created.original_published_day)

    def test_non_url_link_fields_save_as_blank(self):
        job = BookFetchJob.objects.create(requested_count=1)
        records = [_book_record(epub_link="not a url", pdf_link="https://example.com/book.pdf")]

        with patch("apps.story.book_fetch_jobs.fetch_books", return_value=records):
            run_book_fetch(job.id)

        created = StoryQueue.objects.get()
        self.assertEqual(created.epub_link, "")
        self.assertEqual(created.pdf_link, "https://example.com/book.pdf")

    def test_book_fetch_error_marks_job_failed(self):
        job = BookFetchJob.objects.create(requested_count=1)

        with patch("apps.story.book_fetch_jobs.fetch_books", side_effect=BookFetchError("boom")):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.status, BookFetchJob.STATUS_FAILED)
        self.assertEqual(job.error_message, "boom")
        self.assertEqual(StoryQueue.objects.count(), 0)

    def test_unexpected_exception_marks_job_failed_with_generic_message(self):
        job = BookFetchJob.objects.create(requested_count=1)

        with patch("apps.story.book_fetch_jobs.fetch_books", side_effect=RuntimeError("kaboom")):
            run_book_fetch(job.id)

        job.refresh_from_db()
        self.assertEqual(job.status, BookFetchJob.STATUS_FAILED)
        self.assertEqual(job.error_message, "Unexpected internal error.")


class FetchBooksApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin@example.com", username="admin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_fetch_books_returns_202_immediately_without_waiting_on_processing(self):
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.book_fetch_executor") as mock_executor:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post("/api/admin/story-queue/fetch-books/", {"count": 5}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], BookFetchJob.STATUS_PENDING)
        self.assertEqual(response.data["requested_count"], 5)
        mock_executor.submit.assert_called_once()
        self.assertEqual(BookFetchJob.objects.count(), 1)

    def test_omitted_count_defaults(self):
        self.client.force_authenticate(user=self._make_superuser())

        with patch("apps.story.api.book_fetch_executor"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post("/api/admin/story-queue/fetch-books/", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["requested_count"], DEFAULT_BOOK_FETCH_COUNT)

    def test_count_out_of_range_or_non_integer_returns_400(self):
        self.client.force_authenticate(user=self._make_superuser())

        for bad_count in (0, MAX_BOOK_FETCH_COUNT + 1, "not-a-number"):
            response = self.client.post("/api/admin/story-queue/fetch-books/", {"count": bad_count}, format="json")
            self.assertEqual(response.status_code, 400)

        self.assertEqual(BookFetchJob.objects.count(), 0)

    def test_non_superuser_is_forbidden(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)

        response = self.client.post("/api/admin/story-queue/fetch-books/", {"count": 5}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_status_endpoint_returns_the_job_or_404(self):
        self.client.force_authenticate(user=self._make_superuser())
        job = BookFetchJob.objects.create(requested_count=3, status=BookFetchJob.STATUS_COMPLETED, created_count=2)

        response = self.client.get(f"/api/admin/story-queue/fetch-books/{job.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["created_count"], 2)

        missing_response = self.client.get("/api/admin/story-queue/fetch-books/999999/")
        self.assertEqual(missing_response.status_code, 404)


def _csv_upload(rows, filename="books.csv"):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return SimpleUploadedFile(filename, buf.getvalue().encode("utf-8"), content_type="text/csv")


def _xlsx_upload(rows, filename="books.xlsx"):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buf = BytesIO()
    workbook.save(buf)
    return SimpleUploadedFile(
        filename, buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


class QueueImportFileParsingTests(SimpleTestCase):
    def test_csv_headers_are_aliased_and_normalized(self):
        upload = _csv_upload([["Title", "Author", "Year"], ["A Book", "An Author", "1900"]])

        rows = parse_uploaded_file(upload)

        self.assertEqual(rows, [{"title": "A Book", "author_name": "An Author", "original_published_year": "1900"}])

    def test_xlsx_parses_rows(self):
        upload = _xlsx_upload([["title", "author_name"], ["XLSX Book", "XLSX Author"]])

        rows = parse_uploaded_file(upload)

        self.assertEqual(rows, [{"title": "XLSX Book", "author_name": "XLSX Author"}])

    def test_blank_rows_are_skipped(self):
        upload = _csv_upload([["title", "author_name"], ["", ""], ["A Book", "An Author"]])

        rows = parse_uploaded_file(upload)

        self.assertEqual(len(rows), 1)

    def test_bad_extension_raises_import_file_error(self):
        upload = SimpleUploadedFile("books.txt", b"title\nA Book", content_type="text/plain")

        with self.assertRaises(ImportFileError):
            parse_uploaded_file(upload)

    def test_oversized_file_raises_import_file_error(self):
        from apps.story.queue_import import MAX_IMPORT_FILE_SIZE

        oversized = SimpleUploadedFile("books.csv", b"a" * (MAX_IMPORT_FILE_SIZE + 1), content_type="text/csv")

        with self.assertRaises(ImportFileError):
            parse_uploaded_file(oversized)

    def test_too_many_rows_raises_import_file_error(self):
        rows = [["title"]] + [[f"Book {i}"] for i in range(MAX_IMPORT_ROWS + 1)]
        upload = _csv_upload(rows)

        with self.assertRaises(ImportFileError):
            parse_uploaded_file(upload)

    def test_empty_file_raises_import_file_error(self):
        upload = _csv_upload([["title"]])

        with self.assertRaises(ImportFileError):
            parse_uploaded_file(upload)


class BuildPreviewTests(TestCase):
    def test_valid_row_is_resolved_and_added(self):
        upload = _csv_upload(
            [
                ["title", "author_name", "country", "language", "genres", "original_published_year"],
                ["A New Book", "New Author", "Japan", "Japanese", "Adventure; Gothic", "1900"],
            ]
        )

        preview = build_preview(upload)

        self.assertEqual(preview["to_add_count"], 1)
        entry = preview["to_add"][0]
        self.assertEqual(entry["country"], "JP")
        self.assertEqual(entry["language"], "ja")
        self.assertEqual(entry["genres"], ["Adventure", "Gothic"])
        self.assertEqual(entry["published_date_label"], "1900")

    def test_comma_separated_genres_also_split(self):
        upload = _csv_upload(
            [["title", "genres"], ["A Book", "Adventure, Gothic"]]
        )

        preview = build_preview(upload)

        self.assertEqual(preview["to_add"][0]["genres"], ["Adventure", "Gothic"])

    def test_missing_title_is_reported_as_an_error(self):
        upload = _csv_upload([["title", "author_name"], ["", "An Author"]])

        preview = build_preview(upload)

        self.assertEqual(preview["to_add_count"], 0)
        self.assertEqual(preview["error_count"], 1)
        self.assertIn("missing required 'title'", preview["errors"][0])

    def test_row_matching_an_existing_story_is_a_duplicate(self):
        Story.objects.create(
            title="Already Here", slug="already-here", author=Author.objects.create(name="Some Author")
        )
        upload = _csv_upload([["title", "author_name"], ["Already Here", "Some Author"]])

        preview = build_preview(upload)

        self.assertEqual(preview["to_add_count"], 0)
        self.assertEqual(preview["duplicate_count"], 1)
        self.assertEqual(preview["duplicates"][0]["reason"], "already_a_story")

    def test_row_matching_a_not_yet_added_queue_entry_is_a_duplicate(self):
        StoryQueue.objects.create(title="In The Queue", author_name="Queue Author")
        upload = _csv_upload([["title", "author_name"], ["In The Queue", "Queue Author"]])

        preview = build_preview(upload)

        self.assertEqual(preview["duplicates"][0]["reason"], "already_in_queue")

    def test_duplicate_within_the_file_is_flagged(self):
        upload = _csv_upload(
            [["title", "author_name"], ["Same Book", "Same Author"], ["same book", "same author"]]
        )

        preview = build_preview(upload)

        self.assertEqual(preview["to_add_count"], 1)
        self.assertEqual(preview["duplicates"][0]["reason"], "duplicate_in_file")

    def test_preview_does_not_create_genres_or_categories(self):
        upload = _csv_upload([["title", "genres", "categories"], ["A Book", "Brand New Genre", "Brand New Category"]])

        build_preview(upload)

        self.assertEqual(Genre.objects.filter(name="Brand New Genre").count(), 0)
        self.assertEqual(Category.objects.filter(name="Brand New Category").count(), 0)


class ImportApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin@example.com", username="admin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_import_preview_returns_counts(self):
        self.client.force_authenticate(user=self._make_superuser())
        upload = _csv_upload([["title", "author_name"], ["A Book", "An Author"]])

        response = self.client.post("/api/admin/story-queue/import-preview/", {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["to_add_count"], 1)

    def test_import_preview_requires_a_file(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.post("/api/admin/story-queue/import-preview/", {}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_import_preview_rejects_bad_file_type(self):
        self.client.force_authenticate(user=self._make_superuser())
        upload = SimpleUploadedFile("books.txt", b"title\nA Book", content_type="text/plain")

        response = self.client.post("/api/admin/story-queue/import-preview/", {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_import_preview_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)
        upload = _csv_upload([["title"], ["A Book"]])

        response = self.client.post("/api/admin/story-queue/import-preview/", {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 403)

    def test_import_confirm_creates_queue_rows_and_resolves_genres(self):
        self.client.force_authenticate(user=self._make_superuser())
        upload = _csv_upload([["title", "author_name", "genres"], ["A Book", "An Author", "New Genre"]])
        preview_response = self.client.post(
            "/api/admin/story-queue/import-preview/", {"file": upload}, format="multipart"
        )

        response = self.client.post(
            "/api/admin/story-queue/import-confirm/",
            {"records": preview_response.data["to_add"]},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created_count"], 1)
        created = StoryQueue.objects.get(title="A Book")
        self.assertEqual(list(created.genres.values_list("name", flat=True)), ["New Genre"])
        self.assertEqual(Genre.objects.filter(name="New Genre").count(), 1)

    def test_import_confirm_preserves_country_and_language_from_preview(self):
        # Regression test: build_preview resolves country/language name ->
        # code for display; confirm_import must NOT re-resolve that already-
        # resolved code (see queue_records.validate_country_code) or it
        # silently comes back blank.
        self.client.force_authenticate(user=self._make_superuser())
        upload = _csv_upload(
            [["title", "country", "language"], ["A Book", "Japan", "Japanese"]]
        )
        preview_response = self.client.post(
            "/api/admin/story-queue/import-preview/", {"file": upload}, format="multipart"
        )
        self.assertEqual(preview_response.data["to_add"][0]["country"], "JP")
        self.assertEqual(preview_response.data["to_add"][0]["language"], "ja")

        response = self.client.post(
            "/api/admin/story-queue/import-confirm/",
            {"records": preview_response.data["to_add"]},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created_count"], 1)
        created = StoryQueue.objects.get(title="A Book")
        self.assertEqual(created.country, "JP")
        self.assertEqual(created.language, "ja")

    def test_import_confirm_rejects_empty_records(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.post("/api/admin/story-queue/import-confirm/", {"records": []}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_import_confirm_rejects_too_many_records(self):
        self.client.force_authenticate(user=self._make_superuser())
        records = [{"title": f"Book {i}"} for i in range(MAX_IMPORT_ROWS + 1)]

        response = self.client.post("/api/admin/story-queue/import-confirm/", {"records": records}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_import_confirm_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)

        response = self.client.post(
            "/api/admin/story-queue/import-confirm/", {"records": [{"title": "A Book"}]}, format="json"
        )

        self.assertEqual(response.status_code, 403)

    def test_import_confirm_skips_a_record_that_became_a_duplicate_since_preview(self):
        self.client.force_authenticate(user=self._make_superuser())
        upload = _csv_upload([["title", "author_name"], ["A Book", "An Author"]])
        preview_response = self.client.post(
            "/api/admin/story-queue/import-preview/", {"file": upload}, format="multipart"
        )

        # Simulate a race: the same book got added to Story between preview and confirm.
        Story.objects.create(
            title="A Book", slug="a-book", author=Author.objects.create(name="An Author")
        )

        response = self.client.post(
            "/api/admin/story-queue/import-confirm/",
            {"records": preview_response.data["to_add"]},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created_count"], 0)
        self.assertEqual(response.data["skipped_count"], 1)
        self.assertEqual(StoryQueue.objects.filter(title="A Book").count(), 0)


class ResolveStoryMatchTests(TestCase):
    def test_exact_single_match(self):
        story = Story.objects.create(title="A Book", slug="a-book")

        result = resolve_story_match("a book")

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["story"].id, story.id)

    def test_not_found(self):
        result = resolve_story_match("Nonexistent Book")

        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["story"])

    def test_only_considers_published_stories(self):
        Story.objects.create(title="Draft Book", slug="draft-book", is_published=False)

        result = resolve_story_match("Draft Book")

        self.assertEqual(result["status"], "not_found")

    def test_ambiguous_without_author_name(self):
        Story.objects.create(title="Twins", slug="twins-1", author=Author.objects.create(name="Author One"))
        Story.objects.create(title="Twins", slug="twins-2", author=Author.objects.create(name="Author Two"))

        result = resolve_story_match("Twins")

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def test_ambiguous_resolved_by_author_name(self):
        Story.objects.create(title="Twins", slug="twins-1", author=Author.objects.create(name="Author One"))
        correct = Story.objects.create(
            title="Twins", slug="twins-2", author=Author.objects.create(name="Author Two")
        )

        result = resolve_story_match("Twins", "author two")

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["story"].id, correct.id)

    def test_ambiguous_stays_ambiguous_when_author_name_does_not_narrow_to_one(self):
        Story.objects.create(title="Twins", slug="twins-1", author=Author.objects.create(name="Author One"))
        Story.objects.create(title="Twins", slug="twins-2", author=Author.objects.create(name="Author One"))

        result = resolve_story_match("Twins", "author one")

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)


class TaxonomyBulkUpdatePreviewTests(TestCase):
    def test_blank_tags_cell_leaves_tags_unchanged(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        story.tags.add(Tag.objects.create(name="Existing Tag", slug="existing-tag"))
        upload = _csv_upload([["title", "themes"], ["A Book", "New Theme"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertIsNone(row["proposed_tags"])
        self.assertEqual(row["current_tags"], ["Existing Tag"])
        self.assertEqual(row["tags_added"], [])
        self.assertEqual(row["tags_removed"], [])

    def test_blank_themes_cell_leaves_themes_unchanged(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        story.themes.add(Theme.objects.create(name="Existing Theme", slug="existing-theme"))
        upload = _csv_upload([["title", "tags"], ["A Book", "New Tag"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertIsNone(row["proposed_themes"])
        self.assertEqual(row["current_themes"], ["Existing Theme"])
        self.assertEqual(row["themes_added"], [])
        self.assertEqual(row["themes_removed"], [])

    def test_blank_genres_cell_leaves_genres_unchanged(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        story.genres.add(Genre.objects.create(name="Existing Genre"))
        upload = _csv_upload([["title", "tags"], ["A Book", "New Tag"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertIsNone(row["proposed_genres"])
        self.assertEqual(row["current_genres"], ["Existing Genre"])
        self.assertEqual(row["genres_added"], [])
        self.assertEqual(row["genres_removed"], [])

    def test_blank_categories_cell_leaves_categories_unchanged(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        story.categories.add(Category.objects.create(name="Existing Category"))
        upload = _csv_upload([["title", "tags"], ["A Book", "New Tag"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertIsNone(row["proposed_categories"])
        self.assertEqual(row["current_categories"], ["Existing Category"])
        self.assertEqual(row["categories_added"], [])
        self.assertEqual(row["categories_removed"], [])
        self.assertIsNone(row["category_count_warning"])
        self.assertIsNone(row["category_forbidden_value"])

    def test_new_tag_theme_and_genre_are_reported_but_not_created(self):
        Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload(
            [["title", "tags", "themes", "genres"], ["A Book", "Brand New Tag", "Brand New Theme", "Brand New Genre"]]
        )

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertEqual(row["new_tags_to_create"], ["Brand New Tag"])
        self.assertEqual(row["new_themes_to_create"], ["Brand New Theme"])
        self.assertEqual(row["new_genres_to_create"], ["Brand New Genre"])
        self.assertFalse(Tag.objects.filter(name="Brand New Tag").exists())
        self.assertFalse(Theme.objects.filter(name="Brand New Theme").exists())
        self.assertFalse(Genre.objects.filter(name="Brand New Genre").exists())

    def test_new_category_is_flagged_as_never_created(self):
        Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload([["title", "categories"], ["A Book", "Brand New Category"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertEqual(row["new_categories_not_created"], ["Brand New Category"])
        self.assertFalse(Category.objects.filter(name="Brand New Category").exists())

    def test_forbidden_category_is_flagged(self):
        Story.objects.create(title="A Book", slug="a-book")
        Category.objects.create(name="Science Fiction")
        upload = _csv_upload([["title", "categories"], ["A Book", "Science Fiction"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertEqual(row["category_forbidden_value"], "Science Fiction")

    def test_category_count_of_zero_warns(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        story.categories.add(Category.objects.create(name="Existing Category"))
        upload = _csv_upload([["title", "categories"], ["A Book", ","]])  # non-blank cell, splits to []

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertEqual(row["proposed_categories"], [])
        self.assertIsNotNone(row["category_count_warning"])

    def test_category_count_of_three_warns(self):
        Story.objects.create(title="A Book", slug="a-book")
        Category.objects.create(name="Cat One")
        Category.objects.create(name="Cat Two")
        Category.objects.create(name="Cat Three")
        upload = _csv_upload([["title", "categories"], ["A Book", "Cat One; Cat Two; Cat Three"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertIsNotNone(row["category_count_warning"])

    def test_category_count_of_two_does_not_warn(self):
        Story.objects.create(title="A Book", slug="a-book")
        Category.objects.create(name="Cat One")
        Category.objects.create(name="Cat Two")
        upload = _csv_upload([["title", "categories"], ["A Book", "Cat One; Cat Two"]])

        preview = build_taxonomy_preview(upload)

        row = preview["rows"][0]
        self.assertIsNone(row["category_count_warning"])

    def test_missing_title_is_reported_as_an_error(self):
        upload = _csv_upload([["title", "tags"], ["", "A Tag"]])

        preview = build_taxonomy_preview(upload)

        self.assertEqual(len(preview["rows"]), 0)
        self.assertEqual(len(preview["errors"]), 1)
        self.assertIn("missing required 'title'", preview["errors"][0])

    def test_not_found_and_ambiguous_counts(self):
        Story.objects.create(title="Twins", slug="twins-1", author=Author.objects.create(name="A"))
        Story.objects.create(title="Twins", slug="twins-2", author=Author.objects.create(name="B"))
        upload = _csv_upload([["title"], ["Nonexistent"], ["Twins"]])

        preview = build_taxonomy_preview(upload)

        self.assertEqual(preview["not_found_count"], 1)
        self.assertEqual(preview["ambiguous_count"], 1)
        self.assertEqual(preview["matched_count"], 0)
        ambiguous_row = next(r for r in preview["rows"] if r["match_status"] == "ambiguous")
        self.assertEqual(len(ambiguous_row["ambiguous_candidates"]), 2)


class TaxonomyBulkUpdateConfirmTests(TestCase):
    def test_new_tag_theme_genre_are_created_only_at_confirm(self):
        Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload(
            [["title", "tags", "themes", "genres"], ["A Book", "New Tag", "New Theme", "New Genre"]]
        )
        preview = build_taxonomy_preview(upload)
        self.assertFalse(Tag.objects.filter(name="New Tag").exists())

        result = confirm_taxonomy_update(preview["rows"])

        self.assertEqual(result["updated_count"], 1)
        self.assertTrue(Tag.objects.filter(name="New Tag").exists())
        self.assertTrue(Theme.objects.filter(name="New Theme").exists())
        self.assertTrue(Genre.objects.filter(name="New Genre").exists())
        story = Story.objects.get(title="A Book")
        self.assertEqual(list(story.tags.values_list("name", flat=True)), ["New Tag"])

    def test_new_category_is_never_created_at_confirm(self):
        Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload([["title", "categories"], ["A Book", "Brand New Category"]])
        preview = build_taxonomy_preview(upload)

        result = confirm_taxonomy_update(preview["rows"])

        self.assertEqual(result["updated_count"], 1)
        self.assertFalse(Category.objects.filter(name="Brand New Category").exists())
        story = Story.objects.get(title="A Book")
        self.assertEqual(list(story.categories.all()), [])

    def test_forbidden_category_blocks_confirm_even_if_included(self):
        Category.objects.create(name="Science Fiction")
        story = Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload([["title", "categories"], ["A Book", "Science Fiction"]])
        preview = build_taxonomy_preview(upload)
        self.assertIsNotNone(preview["rows"][0]["category_forbidden_value"])

        result = confirm_taxonomy_update(preview["rows"])

        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertTrue(any("Science Fiction" in e for e in result["errors"]))
        story.refresh_from_db()
        self.assertEqual(list(story.categories.all()), [])

    def test_category_count_warning_does_not_block_confirm(self):
        # 0 resulting categories is outside the expected 1-2 range — should
        # warn but still apply (unlike category_forbidden_value, which blocks).
        story = Story.objects.create(title="A Book", slug="a-book")
        story.categories.add(Category.objects.create(name="Old Category"))
        upload = _csv_upload([["title", "categories"], ["A Book", ","]])  # non-blank, splits to []
        preview = build_taxonomy_preview(upload)
        self.assertIsNotNone(preview["rows"][0]["category_count_warning"])

        result = confirm_taxonomy_update(preview["rows"])

        self.assertEqual(result["updated_count"], 1)
        story.refresh_from_db()
        self.assertEqual(list(story.categories.all()), [])

    def test_only_fields_with_a_proposed_value_are_applied(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        story.tags.add(Tag.objects.create(name="Untouched Tag", slug="untouched-tag"))
        upload = _csv_upload([["title", "themes"], ["A Book", "New Theme"]])
        preview = build_taxonomy_preview(upload)

        confirm_taxonomy_update(preview["rows"])

        story.refresh_from_db()
        self.assertEqual(list(story.tags.values_list("name", flat=True)), ["Untouched Tag"])
        self.assertEqual(list(story.themes.values_list("name", flat=True)), ["New Theme"])

    def test_skips_a_row_that_no_longer_matches_since_preview(self):
        story = Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload([["title", "tags"], ["A Book", "A Tag"]])
        preview = build_taxonomy_preview(upload)
        story.is_published = False
        story.save(update_fields=["is_published"])

        result = confirm_taxonomy_update(preview["rows"])

        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["skipped_count"], 1)


class TaxonomyBulkUpdateApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin@example.com", username="admin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_preview_and_confirm_end_to_end(self):
        self.client.force_authenticate(user=self._make_superuser())
        Story.objects.create(title="A Book", slug="a-book")
        upload = _csv_upload([["title", "tags"], ["A Book", "A New Tag"]])

        preview_response = self.client.post(
            "/api/admin/stories/bulk-taxonomy-preview/", {"file": upload}, format="multipart"
        )
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.data["matched_count"], 1)

        confirm_response = self.client.post(
            "/api/admin/stories/bulk-taxonomy-confirm/",
            {"records": preview_response.data["rows"]},
            format="json",
        )

        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(confirm_response.data["updated_count"], 1)
        story = Story.objects.get(title="A Book")
        self.assertEqual(list(story.tags.values_list("name", flat=True)), ["A New Tag"])

    def test_preview_requires_a_file(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.post("/api/admin/stories/bulk-taxonomy-preview/", {}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_preview_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)
        upload = _csv_upload([["title"], ["A Book"]])

        response = self.client.post(
            "/api/admin/stories/bulk-taxonomy-preview/", {"file": upload}, format="multipart"
        )

        self.assertEqual(response.status_code, 403)

    def test_confirm_rejects_empty_records(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.post(
            "/api/admin/stories/bulk-taxonomy-confirm/", {"records": []}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_confirm_rejects_too_many_records(self):
        self.client.force_authenticate(user=self._make_superuser())
        records = [{"title": f"Book {i}"} for i in range(MAX_IMPORT_ROWS + 1)]

        response = self.client.post(
            "/api/admin/stories/bulk-taxonomy-confirm/", {"records": records}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_confirm_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)

        response = self.client.post(
            "/api/admin/stories/bulk-taxonomy-confirm/", {"records": [{"title": "A Book"}]}, format="json"
        )

        self.assertEqual(response.status_code, 403)


class StoryExportApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin@example.com", username="admin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_export_returns_csv_with_expected_header_and_content_type(self):
        self.client.force_authenticate(user=self._make_superuser())

        response = self.client.get("/api/admin/stories/export/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(
            rows[0],
            [
                "title", "author_name", "about", "story_type", "country", "language", "genres", "categories",
                "tags", "themes", "original_published_year", "original_published_month",
                "original_published_day", "epub_link", "pdf_link", "cover_image_link",
            ],
        )

    def test_export_includes_resolved_names_and_genre_category_lists(self):
        self.client.force_authenticate(user=self._make_superuser())
        author = Author.objects.create(name="Some Author")
        story = Story.objects.create(
            title="A Book", slug="a-book", author=author, country="JP", language="ja",
            about="A synopsis.", original_published_year=1900,
        )
        story.genres.add(Genre.objects.create(name="Adventure"), Genre.objects.create(name="Gothic"))
        story.categories.add(Category.objects.create(name="Classic Literature"))
        story.tags.add(Tag.objects.create(name="A Tag", slug="a-tag"))
        story.themes.add(Theme.objects.create(name="A Theme", slug="a-theme"))

        response = self.client.get("/api/admin/stories/export/")

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        row = rows[1]
        self.assertEqual(row[0], "A Book")
        self.assertEqual(row[1], "Some Author")
        self.assertEqual(row[4], "Japan")
        self.assertEqual(row[5], "Japanese")
        self.assertEqual(row[6], "Adventure, Gothic")
        self.assertEqual(row[7], "Classic Literature")
        self.assertEqual(row[8], "A Tag")
        self.assertEqual(row[9], "A Theme")
        self.assertEqual(row[10], "1900")

    def test_export_respects_is_published_filter(self):
        self.client.force_authenticate(user=self._make_superuser())
        Story.objects.create(title="Published", slug="published", is_published=True)
        Story.objects.create(title="Draft", slug="draft", is_published=False)

        response = self.client.get("/api/admin/stories/export/", {"is_published": "true"})

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        titles = [row[0] for row in rows[1:]]
        self.assertEqual(titles, ["Published"])

    def test_export_includes_not_added_story_queue_entries_by_default(self):
        self.client.force_authenticate(user=self._make_superuser())
        StoryQueue.objects.create(title="Not A Real Story Yet", author_name="Someone")

        response = self.client.get("/api/admin/stories/export/")

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        titles = [row[0] for row in rows[1:]]
        self.assertEqual(titles, ["Not A Real Story Yet"])

    def test_export_omits_already_added_queue_entries(self):
        self.client.force_authenticate(user=self._make_superuser())
        added_story = Story.objects.create(title="Already Published", slug="already-published")
        StoryQueue.objects.create(
            title="Already Added", author_name="Someone", is_added=True, added_story=added_story
        )

        response = self.client.get("/api/admin/stories/export/", {"include_stories": "false"})

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len(rows), 1)  # header only — is_added=True queue entries are never in scope

    def test_export_include_stories_false_omits_story_rows(self):
        self.client.force_authenticate(user=self._make_superuser())
        Story.objects.create(title="A Published Story", slug="a-published-story")
        StoryQueue.objects.create(title="A Queue Entry", author_name="Someone")

        response = self.client.get("/api/admin/stories/export/", {"include_stories": "false"})

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        titles = [row[0] for row in rows[1:]]
        self.assertEqual(titles, ["A Queue Entry"])

    def test_export_include_queue_false_omits_queue_rows(self):
        self.client.force_authenticate(user=self._make_superuser())
        Story.objects.create(title="A Published Story", slug="a-published-story")
        StoryQueue.objects.create(title="A Queue Entry", author_name="Someone")

        response = self.client.get("/api/admin/stories/export/", {"include_queue": "false"})

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        titles = [row[0] for row in rows[1:]]
        self.assertEqual(titles, ["A Published Story"])

    def test_export_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)

        response = self.client.get("/api/admin/stories/export/")

        self.assertEqual(response.status_code, 403)


class ResolveStoryTypeTests(TestCase):
    def test_matches_case_insensitively(self):
        matched = resolve_story_type("novel")
        self.assertEqual(matched.name, "Novel")

    def test_does_not_create_a_new_type_by_default(self):
        matched = resolve_story_type("Not A Real Type")
        self.assertIsNone(matched)
        self.assertFalse(StoryType.objects.filter(name="Not A Real Type").exists())

    def test_create_missing_true_creates_a_new_type(self):
        matched = resolve_story_type("Brand New Type", create_missing=True)
        self.assertEqual(matched.name, "Brand New Type")
        self.assertTrue(StoryType.objects.filter(name="Brand New Type").exists())

    def test_blank_returns_none(self):
        self.assertIsNone(resolve_story_type("  "))


class StoryTypeViewSetTests(APITestCase):
    def test_public_list_returns_all_types_ordered_by_name(self):
        response = self.client.get("/api/story-types/")

        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.data]
        self.assertEqual(names, sorted(names))
        self.assertIn("Novel", names)


class StoryTypeAdminApiTests(APITestCase):
    def _make_superuser(self):
        return User.objects.create(
            email="admin@example.com", username="admin", is_superuser=True, is_staff=True, is_active=True
        )

    def test_create_update_and_list(self):
        self.client.force_authenticate(user=self._make_superuser())

        create_response = self.client.post("/api/admin/story-types/", {"name": "Fable"}, format="json")
        self.assertEqual(create_response.status_code, 201)
        story_type_id = create_response.data["id"]

        update_response = self.client.patch(
            f"/api/admin/story-types/{story_type_id}/", {"name": "Fables"}, format="json"
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.data["name"], "Fables")

        list_response = self.client.get("/api/admin/story-types/")
        self.assertIn("Fables", [item["name"] for item in list_response.data])

    def test_delete_succeeds_when_not_in_use(self):
        self.client.force_authenticate(user=self._make_superuser())
        story_type = StoryType.objects.create(name="Unused Type")

        response = self.client.delete(f"/api/admin/story-types/{story_type.id}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(StoryType.objects.filter(id=story_type.id).exists())

    def test_delete_blocked_when_referenced_by_a_story(self):
        self.client.force_authenticate(user=self._make_superuser())
        story_type = StoryType.objects.create(name="In Use Type")
        Story.objects.create(title="A Story", slug="a-story", story_type=story_type)

        response = self.client.delete(f"/api/admin/story-types/{story_type.id}/")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(StoryType.objects.filter(id=story_type.id).exists())

    def test_delete_blocked_when_referenced_by_a_queue_item(self):
        self.client.force_authenticate(user=self._make_superuser())
        story_type = StoryType.objects.create(name="In Use Type")
        StoryQueue.objects.create(title="A Book", story_type=story_type)

        response = self.client.delete(f"/api/admin/story-types/{story_type.id}/")

        self.assertEqual(response.status_code, 400)

    def test_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)

        response = self.client.get("/api/admin/story-types/")

        self.assertEqual(response.status_code, 403)


class StoryCompletionScreenApiTests(APITestCase):
    """The end-of-story screen's payload. Composed from the existing
    "Because you finished" path rather than a second recommendation system."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="completer@example.com", username="completer", password="test-password"
        )
        self.genre = Genre.objects.create(name="Folklore", slug="folklore-completion")
        self.finished = self._story("finished-tale", country="JP", minutes=20)
        self.finished.genres.add(self.genre)

    def _story(self, slug, country="", minutes=None, rating=0):
        story = Story.objects.create(
            title=slug.replace("-", " ").title(),
            slug=slug,
            is_published=True,
            country=country,
            rating=rating,
        )
        if minutes is not None:
            Story.objects.filter(pk=story.pk).update(cached_chapter_reading_minutes=minutes)
            story.refresh_from_db()
        return story

    def _get(self):
        return self.client.get(f"/api/stories/{self.finished.slug}/completion/")

    def test_it_works_for_an_anonymous_reader(self):
        """Someone can finish a story without an account; the screen still has
        to render for them, with the generic similarity ranking."""
        other = self._story("anon-similar", country="JP", minutes=20)
        other.genres.add(self.genre)

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["primary"])
        self.assertEqual(response.data["primary"]["slug"], other.slug)

    def test_it_resolves_the_country_name_for_the_section_heading(self):
        # Two, because the first becomes the primary pick and is then excluded
        # from every section.
        for index in range(2):
            sibling = self._story(f"japan-sibling-{index}", country="JP")
            sibling.genres.add(self.genre)
        self.client.force_authenticate(self.user)

        response = self._get()

        self.assertEqual(response.data["country"], "JP")
        self.assertEqual(response.data["country_name"], "Japan")
        country_section = next(
            section for section in response.data["sections"]
            if section["key"] == "more_from_country"
        )
        self.assertEqual(country_section["title"], "More from Japan")

    def test_the_primary_pick_is_never_repeated_in_a_section(self):
        for index in range(5):
            sibling = self._story(f"jp-sibling-{index}", country="JP", minutes=20)
            sibling.genres.add(self.genre)
        self.client.force_authenticate(self.user)

        response = self._get()

        primary_slug = response.data["primary"]["slug"]
        for section in response.data["sections"]:
            slugs = [story["slug"] for story in section["stories"]]
            self.assertNotIn(primary_slug, slugs, section["key"])

    def test_it_never_recommends_the_story_just_finished(self):
        sibling = self._story("another-tale", country="JP", minutes=20)
        sibling.genres.add(self.genre)
        self.client.force_authenticate(self.user)

        response = self._get()

        self.assertNotEqual(response.data["primary"]["slug"], self.finished.slug)
        for section in response.data["sections"]:
            slugs = [story["slug"] for story in section["stories"]]
            self.assertNotIn(self.finished.slug, slugs)

    def test_similar_length_stays_within_a_proportional_window(self):
        # Three, because one becomes the primary pick and is then excluded from
        # every section.
        close = self._story("about-the-same", minutes=22)
        also_close = self._story("also-about-the-same", minutes=19)
        far = self._story("much-longer", minutes=200)
        for story in (close, also_close, far):
            story.genres.add(self.genre)
        self.client.force_authenticate(self.user)

        response = self._get()

        section = next(
            (s for s in response.data["sections"] if s["key"] == "similar_length"), None
        )
        self.assertIsNotNone(section)
        slugs = [story["slug"] for story in section["stories"]]
        primary_slug = response.data["primary"]["slug"]
        self.assertIn(primary_slug, {close.slug, also_close.slug})
        # Whichever of the two similar-length stories wasn't picked as primary
        # is still in the section; the 200-minute one never is.
        self.assertEqual(
            set(slugs), {close.slug, also_close.slug} - {primary_slug}
        )
        self.assertNotIn(far.slug, slugs)

    def test_sections_with_nothing_in_them_are_omitted(self):
        """The screen renders what exists rather than padding with filler."""
        countryless = self._story("no-country-tale", minutes=20)
        countryless.genres.add(self.genre)
        self.finished.country = ""
        self.finished.save(update_fields=["country"])
        self.client.force_authenticate(self.user)

        response = self._get()

        keys = {section["key"] for section in response.data["sections"]}
        self.assertNotIn("more_from_country", keys)
        self.assertIsNone(response.data["country_name"])

    def test_an_empty_catalogue_returns_no_primary_rather_than_failing(self):
        self.client.force_authenticate(self.user)

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["primary"])
        self.assertEqual(response.data["sections"], [])

    def test_the_sections_exclude_stories_the_reader_has_already_engaged_with(self):
        already_read = self._story("already-read", country="JP", minutes=20)
        already_read.genres.add(self.genre)
        Favorite.objects.create(user=self.user, story=already_read)
        fresh = self._story("not-yet-read", country="JP", minutes=20)
        fresh.genres.add(self.genre)
        self.client.force_authenticate(self.user)

        response = self._get()

        self.assertEqual(response.data["primary"]["slug"], fresh.slug)
        for section in response.data["sections"]:
            slugs = [story["slug"] for story in section["stories"]]
            self.assertNotIn(already_read.slug, slugs, section["key"])

    def test_the_primary_falls_back_to_an_engaged_story_rather_than_nothing(self):
        """§2.2: avoid already-completed stories *unless no alternatives exist*.
        A reader who has genuinely read everything similar should still get a
        suggestion, not an empty panel."""
        already_read = self._story("only-option", country="JP", minutes=20)
        already_read.genres.add(self.genre)
        Favorite.objects.create(user=self.user, story=already_read)
        self.client.force_authenticate(self.user)

        response = self._get()

        self.assertEqual(response.data["primary"]["slug"], already_read.slug)
        # It is offered as the "read next" pick, but never padded into the
        # browse sections alongside it.
        for section in response.data["sections"]:
            slugs = [story["slug"] for story in section["stories"]]
            self.assertNotIn(already_read.slug, slugs, section["key"])


class SurpriseMeApiTests(APITestCase):
    """One story, at random from a shortlist the existing ranking produced —
    a uniformly random story from the whole catalogue is not a surprise."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="surprised@example.com", username="surprised", password="test-password"
        )

    def _story(self, slug, minutes=None, rating=0.0):
        story = Story.objects.create(
            title=slug, slug=slug, is_published=True, rating=rating
        )
        if minutes is not None:
            Story.objects.filter(pk=story.pk).update(cached_chapter_reading_minutes=minutes)
            story.refresh_from_db()
        return story

    def _surprise(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return self.client.get(f"/api/stories/surprise/?{query}")

    def test_it_returns_a_story_for_a_signed_out_reader(self):
        self._story("something-good", rating=4.5)

        response = self._surprise()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["story"]["slug"], "something-good")

    def test_it_never_suggests_the_story_being_read(self):
        current = self._story("the-one-im-reading")
        self._story("a-different-one")

        for _ in range(5):
            response = self._surprise(exclude=current.slug)
            self.assertEqual(response.data["story"]["slug"], "a-different-one")

    def test_it_returns_nothing_rather_than_the_excluded_story(self):
        current = self._story("the-only-story")

        response = self._surprise(exclude=current.slug)

        self.assertIsNone(response.data["story"])

    def test_it_prefers_something_unread(self):
        finished = self._story("already-finished", rating=5.0)
        self._story("not-yet-read", rating=1.0)
        StoryCompletion.objects.create(
            user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
        )
        self.client.force_authenticate(self.user)

        for _ in range(5):
            self.assertEqual(self._surprise().data["story"]["slug"], "not-yet-read")

    def test_it_falls_back_to_a_finished_story_rather_than_nothing(self):
        """§8.1: exclude completed *where possible*. A reader who has finished
        everything should still get a suggestion."""
        finished = self._story("the-only-one")
        StoryCompletion.objects.create(
            user=self.user, story=finished, source=StoryCompletion.SOURCE_CHAPTERS
        )
        self.client.force_authenticate(self.user)

        self.assertEqual(self._surprise().data["story"]["slug"], "the-only-one")

    def test_the_time_filter_keeps_its_promise(self):
        self._story("a-quick-one", minutes=4)
        self._story("a-long-one", minutes=90)

        for _ in range(5):
            response = self._surprise(max_minutes=5)
            self.assertEqual(response.data["story"]["slug"], "a-quick-one")

    def test_it_says_nothing_rather_than_break_the_time_promise(self):
        """"Surprise me, in under five minutes" with nothing that short is
        honestly answered with nothing, not with something twice as long."""
        self._story("a-long-one", minutes=90)

        self.assertIsNone(self._surprise(max_minutes=5).data["story"])

    def test_a_story_of_unknown_length_is_never_offered_under_a_time_promise(self):
        self._story("length-unknown")

        self.assertIsNone(self._surprise(max_minutes=10).data["story"])

    def test_an_unparseable_time_filter_is_ignored_rather_than_fatal(self):
        self._story("anything")

        self.assertIsNotNone(self._surprise(max_minutes="soon").data["story"])

    def test_it_varies_between_calls(self):
        for index in range(12):
            self._story(f"option-{index}", rating=4.0)

        seen = {self._surprise().data["story"]["slug"] for _ in range(15)}

        self.assertGreater(len(seen), 1)


class MoodApiTests(APITestCase):
    """The mood layer, and the review rule that keeps unreviewed machine
    guesses out of what readers see (§8.5)."""

    def setUp(self):
        cache.clear()
        self.funny = Mood.objects.get(slug="funny")
        self.scary = Mood.objects.get(slug="scary")

    def _story(self, slug):
        return Story.objects.create(title=slug, slug=slug, is_published=True)

    def test_the_vocabulary_is_seeded_by_migration(self):
        slugs = set(Mood.objects.filter(active=True).values_list("slug", flat=True))

        self.assertEqual(len(slugs), 10)
        self.assertIn("thought-provoking", slugs)

    def test_moods_do_not_replace_genres_or_categories(self):
        """§8.3 — the existing taxonomies are untouched by this layer."""
        story = self._story("still-has-genres")
        genre = Genre.objects.create(name="Folklore", slug="folklore-mood-test")
        story.genres.add(genre)
        StoryMood.objects.create(story=story, mood=self.funny)

        story.refresh_from_db()
        self.assertEqual(list(story.genres.values_list("slug", flat=True)), ["folklore-mood-test"])
        self.assertEqual(list(story.moods.values_list("slug", flat=True)), ["funny"])

    def test_an_administrator_assignment_is_visible_immediately(self):
        story = self._story("set-by-hand")
        StoryMood.objects.create(story=story, mood=self.funny, source=StoryMood.SOURCE_ADMIN)

        response = self.client.get("/api/stories/?moods=funny")

        self.assertEqual(
            [row["slug"] for row in response.data["results"]], ["set-by-hand"]
        )

    def test_an_unreviewed_ai_suggestion_is_stored_but_never_shown(self):
        """The point of §8.5: a bulk classification run must not leak into
        readers' browsing before anyone has looked at it."""
        story = self._story("machine-guessed")
        assignment = StoryMood.objects.create(
            story=story, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=False
        )

        response = self.client.get("/api/stories/?moods=funny")

        self.assertEqual(response.data["results"], [])
        self.assertTrue(StoryMood.objects.filter(pk=assignment.pk).exists())
        self.assertFalse(assignment.is_public)

    def test_a_reviewed_ai_suggestion_becomes_visible(self):
        story = self._story("machine-then-confirmed")
        StoryMood.objects.create(
            story=story, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=True
        )

        response = self.client.get("/api/stories/?moods=funny")

        self.assertEqual(
            [row["slug"] for row in response.data["results"]], ["machine-then-confirmed"]
        )

    def test_the_mood_list_counts_only_what_readers_can_see(self):
        shown = self._story("counted")
        hidden = self._story("not-counted")
        StoryMood.objects.create(story=shown, mood=self.funny, source=StoryMood.SOURCE_ADMIN)
        StoryMood.objects.create(
            story=hidden, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=False
        )

        response = self.client.get(reverse("mood-list"))

        counts = {row["slug"]: row["stories_count"] for row in response.data["moods"]}
        # An inflated count would lead a reader to a mood whose stories then
        # do not appear.
        self.assertEqual(counts["funny"], 1)
        self.assertEqual(counts["scary"], 0)

    def test_an_unpublished_story_is_not_counted(self):
        story = Story.objects.create(title="Draft", slug="draft-mood", is_published=False)
        StoryMood.objects.create(story=story, mood=self.scary, source=StoryMood.SOURCE_ADMIN)

        response = self.client.get(reverse("mood-list"))

        counts = {row["slug"]: row["stories_count"] for row in response.data["moods"]}
        self.assertEqual(counts["scary"], 0)

    def test_a_story_can_carry_several_moods(self):
        story = self._story("both")
        StoryMood.objects.create(story=story, mood=self.funny)
        StoryMood.objects.create(story=story, mood=self.scary)

        for slug in ("funny", "scary"):
            response = self.client.get(f"/api/stories/?moods={slug}")
            self.assertEqual([row["slug"] for row in response.data["results"]], ["both"])

    def test_filtering_by_several_moods_returns_each_story_once(self):
        story = self._story("tagged-twice")
        StoryMood.objects.create(story=story, mood=self.funny)
        StoryMood.objects.create(story=story, mood=self.scary)

        response = self.client.get("/api/stories/?moods=funny,scary")

        self.assertEqual([row["slug"] for row in response.data["results"]], ["tagged-twice"])

    def test_an_unknown_mood_matches_nothing_rather_than_everything(self):
        self._story("some-story")

        response = self.client.get("/api/stories/?moods=nonsense")

        self.assertEqual(response.data["results"], [])

    def test_an_empty_mood_filter_is_ignored(self):
        self._story("some-story")

        response = self.client.get("/api/stories/?moods=")

        self.assertEqual(len(response.data["results"]), 1)


class MoodAdminApiTests(APITestCase):
    """The admin surface for §8.5 — without somewhere to review suggestions,
    "allow admin review" is a database column rather than a workflow."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="moodadmin@example.com", username="moodadmin", password="test-password",
            is_superuser=True, is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="moodreader@example.com", username="moodreader", password="test-password"
        )
        self.client.force_authenticate(self.admin)
        self.funny = Mood.objects.get(slug="funny")
        self.scary = Mood.objects.get(slug="scary")
        self.story = Story.objects.create(title="A Story", slug="a-mood-story", is_published=True)

    def test_it_lists_the_vocabulary_with_both_counts(self):
        StoryMood.objects.create(story=self.story, mood=self.funny, source=StoryMood.SOURCE_ADMIN)
        other = Story.objects.create(title="Other", slug="other-mood", is_published=True)
        StoryMood.objects.create(
            story=other, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=False
        )

        response = self.client.get(reverse("admin-mood-list"))

        rows = {row["slug"]: row for row in response.data}
        # The visible count matches what readers see; pending is the other half
        # of the picture.
        self.assertEqual(rows["funny"]["stories_count"], 1)
        self.assertEqual(rows["funny"]["pending_review_count"], 1)

    def test_an_admin_can_create_a_mood_and_gets_a_slug(self):
        response = self.client.post(
            reverse("admin-mood-list"), {"name": "Bittersweet", "icon": "🍂"}, format="json"
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["slug"], "bittersweet")

    def test_creating_two_moods_with_the_same_name_does_not_collide(self):
        self.client.post(reverse("admin-mood-list"), {"name": "Bittersweet"}, format="json")
        second = self.client.post(reverse("admin-mood-list"), {"name": "Bittersweet"}, format="json")

        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["slug"], "bittersweet-2")

    def test_assigning_moods_to_a_story_replaces_the_whole_set(self):
        url = reverse("admin-mood-assignments", kwargs={"story_id": self.story.id})
        self.client.post(url, {"moods": [self.funny.id, self.scary.id]}, format="json")

        response = self.client.post(url, {"moods": [self.scary.id]}, format="json")

        self.assertEqual([row["mood_slug"] for row in response.data], ["scary"])

    def test_an_admin_assignment_is_public_immediately(self):
        url = reverse("admin-mood-assignments", kwargs={"story_id": self.story.id})

        response = self.client.post(url, {"moods": [self.funny.id]}, format="json")

        self.assertEqual(response.data[0]["source"], StoryMood.SOURCE_ADMIN)
        self.assertTrue(response.data[0]["is_public"])

    def test_re_saving_a_story_does_not_relabel_a_reviewed_suggestion(self):
        """Otherwise the review record erases itself the first time anyone
        touches the story."""
        StoryMood.objects.create(
            story=self.story, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=True,
            note="checked by an editor",
        )
        url = reverse("admin-mood-assignments", kwargs={"story_id": self.story.id})

        response = self.client.post(url, {"moods": [self.funny.id]}, format="json")

        self.assertEqual(response.data[0]["source"], StoryMood.SOURCE_AI)
        self.assertTrue(response.data[0]["reviewed"])
        self.assertEqual(response.data[0]["note"], "checked by an editor")

    def test_an_unknown_mood_id_is_ignored_rather_than_fatal(self):
        url = reverse("admin-mood-assignments", kwargs={"story_id": self.story.id})

        response = self.client.post(url, {"moods": [self.funny.id, 99999]}, format="json")

        self.assertEqual([row["mood_slug"] for row in response.data], ["funny"])

    def test_assignments_rejects_a_non_list_payload(self):
        url = reverse("admin-mood-assignments", kwargs={"story_id": self.story.id})

        response = self.client.post(url, {"moods": "funny"}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_the_review_queue_shows_only_unreviewed_suggestions(self):
        pending = StoryMood.objects.create(
            story=self.story, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=False
        )
        StoryMood.objects.create(
            story=self.story, mood=self.scary, source=StoryMood.SOURCE_AI, reviewed=True
        )

        response = self.client.get(reverse("admin-mood-pending-review"))

        self.assertEqual([row["id"] for row in response.data], [pending.id])
        self.assertEqual(response.data[0]["story_title"], "A Story")

    def test_approving_a_suggestion_makes_it_visible_to_readers(self):
        pending = StoryMood.objects.create(
            story=self.story, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=False
        )

        response = self.client.post(
            reverse("admin-mood-review-assignment", kwargs={"assignment_id": pending.id}),
            {"approved": True},
            format="json",
        )

        self.assertTrue(response.data["reviewed"])
        self.assertTrue(response.data["is_public"])
        public = self.client.get("/api/stories/?moods=funny")
        self.assertEqual([row["slug"] for row in public.data["results"]], ["a-mood-story"])

    def test_rejecting_a_suggestion_removes_it(self):
        pending = StoryMood.objects.create(
            story=self.story, mood=self.funny, source=StoryMood.SOURCE_AI, reviewed=False
        )

        response = self.client.post(
            reverse("admin-mood-review-assignment", kwargs={"assignment_id": pending.id}),
            {"approved": False},
            format="json",
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(StoryMood.objects.filter(pk=pending.id).exists())

    def test_the_whole_mood_admin_is_superuser_only(self):
        self.client.force_authenticate(self.reader)

        for url in (
            reverse("admin-mood-list"),
            reverse("admin-mood-pending-review"),
        ):
            self.assertEqual(self.client.get(url, format="json").status_code, 403, url)


class StoryJourneyAdminApiTests(APITestCase):
    """Journey management from the custom admin panel — Django admin is
    unusable on this deployment (Django 5.0 on Python 3.14)."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="journeyadmin@example.com", username="journeyadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="journeyreader@example.com", username="journeyreader", password="test-password"
        )
        self.client.force_authenticate(self.admin)
        self.journey = StoryJourney.objects.create(title="A Path", slug="a-path")
        self.first = Story.objects.create(title="One", slug="j-one", is_published=True)
        self.second = Story.objects.create(title="Two", slug="j-two", is_published=True)

    def _items_url(self):
        return reverse("admin-journey-items", kwargs={"pk": self.journey.id})

    def test_it_creates_a_journey_with_a_slug(self):
        response = self.client.post(
            reverse("admin-journey-list"),
            {"title": "Ghost Stories", "description": "Spooky."},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["slug"], "ghost-stories")
        # New journeys stay off until an editor has filled and checked them.
        self.assertFalse(response.data["active"])

    def test_two_journeys_with_the_same_title_do_not_collide(self):
        self.client.post(reverse("admin-journey-list"), {"title": "Ghost Stories"}, format="json")
        second = self.client.post(
            reverse("admin-journey-list"), {"title": "Ghost Stories"}, format="json"
        )

        self.assertEqual(second.data["slug"], "ghost-stories-2")

    def test_position_comes_from_the_order_sent(self):
        """Reordering therefore needs no separate endpoint."""
        response = self.client.put(
            self._items_url(),
            {"items": [{"story": self.second.id}, {"story": self.first.id}]},
            format="json",
        )

        self.assertEqual(
            [(row["story_slug"], row["position"]) for row in response.data],
            [("j-two", 1), ("j-one", 2)],
        )

    def test_replacing_the_set_removes_what_was_dropped(self):
        self.client.put(
            self._items_url(),
            {"items": [{"story": self.first.id}, {"story": self.second.id}]},
            format="json",
        )

        response = self.client.put(
            self._items_url(), {"items": [{"story": self.second.id}]}, format="json"
        )

        self.assertEqual([row["story_slug"] for row in response.data], ["j-two"])

    def test_required_is_carried_through(self):
        response = self.client.put(
            self._items_url(),
            {
                "items": [
                    {"story": self.first.id, "required": True},
                    {"story": self.second.id, "required": False},
                ]
            },
            format="json",
        )

        self.assertEqual([row["required"] for row in response.data], [True, False])

    def test_a_story_listed_twice_is_kept_once(self):
        """A duplicate is an editing slip, not a path."""
        response = self.client.put(
            self._items_url(),
            {"items": [{"story": self.first.id}, {"story": self.first.id}]},
            format="json",
        )

        self.assertEqual(len(response.data), 1)

    def test_an_unknown_story_is_ignored_rather_than_fatal(self):
        response = self.client.put(
            self._items_url(),
            {"items": [{"story": self.first.id}, {"story": 999999}]},
            format="json",
        )

        self.assertEqual([row["story_slug"] for row in response.data], ["j-one"])

    def test_it_rejects_a_malformed_payload(self):
        self.assertEqual(
            self.client.put(self._items_url(), {"items": "nope"}, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.put(
                self._items_url(), {"items": [{"story": "abc"}]}, format="json"
            ).status_code,
            400,
        )

    def test_the_list_reports_both_counts(self):
        self.client.put(
            self._items_url(),
            {
                "items": [
                    {"story": self.first.id, "required": True},
                    {"story": self.second.id, "required": False},
                ]
            },
            format="json",
        )

        row = next(
            item for item in self.client.get(reverse("admin-journey-list")).data
            if item["slug"] == "a-path"
        )
        # Required count is the one that decides completion, so an editor needs
        # to see it separately from the total.
        self.assertEqual((row["item_count"], row["required_count"]), (2, 1))

    def test_activating_a_journey_shows_it_to_readers(self):
        self.client.put(self._items_url(), {"items": [{"story": self.first.id}]}, format="json")
        self.client.patch(
            reverse("admin-journey-detail", kwargs={"pk": self.journey.id}),
            {"active": True},
            format="json",
        )

        self.client.force_authenticate(None)
        public = self.client.get(reverse("journey-list"), format="json").data["journeys"]

        self.assertEqual([row["slug"] for row in public], ["a-path"])

    def test_journey_administration_is_superuser_only(self):
        self.client.force_authenticate(self.reader)

        self.assertEqual(
            self.client.get(reverse("admin-journey-list"), format="json").status_code, 403
        )


class StoryJourneyNextStoryApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.reader = User.objects.create_user(
            email="pathreader@example.com", username="pathreader", password="test-password"
        )

    def _story(self, slug, *, published=True):
        return Story.objects.create(
            title=slug.replace("-", " ").title(),
            slug=slug,
            is_published=published,
            cover_image=f"https://example.com/{slug}.jpg",
        )

    def _journey(self, slug, stories):
        journey = StoryJourney.objects.create(title=slug.title(), slug=slug, active=True)
        for position, (story, required) in enumerate(stories, start=1):
            StoryJourneyItem.objects.create(
                journey=journey, story=story, position=position, required=required
            )
        return journey

    def _list_row(self, slug):
        rows = self.client.get(reverse("journey-list"), format="json").data["journeys"]
        return next(row for row in rows if row["slug"] == slug)

    def test_anonymous_reader_gets_first_required_published_story(self):
        first = self._story("first-stop")
        self._journey("anonymous-path", [(first, True)])

        row = self._list_row("anonymous-path")

        self.assertEqual(row["next_story"]["slug"], first.slug)
        detail = self.client.get(
            reverse("journey-detail", kwargs={"slug": "anonymous-path"}), format="json"
        )
        self.assertEqual(detail.data["next_story"]["slug"], first.slug)

    def test_reader_with_first_completed_gets_second(self):
        first = self._story("finished-stop")
        second = self._story("next-stop")
        self._journey("reader-path", [(first, True), (second, True)])
        StoryCompletion.objects.create(
            user=self.reader, story=first, source=StoryCompletion.SOURCE_BACKFILL
        )
        self.client.force_authenticate(self.reader)

        self.assertEqual(self._list_row("reader-path")["next_story"]["slug"], second.slug)

    def test_unpublished_story_is_skipped(self):
        hidden = self._story("hidden-stop", published=False)
        visible = self._story("visible-stop")
        self._journey("published-path", [(hidden, True), (visible, True)])

        self.assertEqual(self._list_row("published-path")["next_story"]["slug"], visible.slug)

    def test_optional_items_are_skipped(self):
        optional = self._story("bonus-stop")
        required = self._story("required-stop")
        self._journey("required-path", [(optional, False), (required, True)])

        self.assertEqual(self._list_row("required-path")["next_story"]["slug"], required.slug)

    def test_complete_journey_returns_null(self):
        story = self._story("last-stop")
        self._journey("complete-path", [(story, True)])
        StoryCompletion.objects.create(
            user=self.reader, story=story, source=StoryCompletion.SOURCE_BACKFILL
        )
        self.client.force_authenticate(self.reader)

        self.assertIsNone(self._list_row("complete-path")["next_story"])

    def test_detail_next_story_follows_the_reader_and_is_null_when_complete(self):
        first = self._story("detail-first")
        second = self._story("detail-second")
        self._journey("detail-path", [(first, True), (second, True)])
        url = reverse("journey-detail", kwargs={"slug": "detail-path"})
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(url).data["next_story"]["slug"], first.slug)
        for story in (first, second):
            StoryCompletion.objects.create(user=self.reader, story=story, source=StoryCompletion.SOURCE_BACKFILL)
        self.assertIsNone(self.client.get(url).data["next_story"])

    def test_unpublished_required_story_does_not_count_toward_the_journey(self):
        live = self._story("live-stop")
        hidden = self._story("unpublished-stop", published=False)
        self._journey("count-path", [(live, True), (hidden, True)])
        self.client.force_authenticate(self.reader)
        row = self._list_row("count-path")
        self.assertEqual((row["completed"], row["total"], row["is_complete"]), (0, 1, False))
        StoryCompletion.objects.create(user=self.reader, story=live, source=StoryCompletion.SOURCE_BACKFILL)
        row = self._list_row("count-path")
        self.assertEqual((row["completed"], row["total"], row["is_complete"]), (1, 1, True))
        self.assertIsNone(row["next_story"])
        detail = self.client.get(reverse("journey-detail", kwargs={"slug": "count-path"})).data
        self.assertEqual([item["story"]["slug"] for item in detail["items"]], ["live-stop"])
        self.assertEqual((detail["total"], detail["is_complete"]), (1, True))

    def test_scheduled_required_story_is_not_counted_until_it_is_live(self):
        live = self._story("sched-live")
        later = self._story("sched-later")
        Story.objects.filter(pk=later.pk).update(publish_at=timezone.now() + timedelta(days=2))
        self._journey("sched-path", [(live, True), (later, True)])
        self.assertEqual(self._list_row("sched-path")["total"], 1)

    def test_next_story_carries_title_and_card_cover(self):
        story = self._story("cover-stop")
        self._journey("cover-path", [(story, True)])
        row = self._list_row("cover-path")["next_story"]
        self.assertEqual(set(row), {"slug", "title", "cover_image"})
        self.assertEqual(row["title"], "Cover Stop")
        self.assertEqual(row["cover_image"], "https://example.com/cover-stop.jpg")

    def test_list_query_count_is_constant_as_journeys_grow(self):
        first = self._story("query-base")
        self._journey("query-path-0", [(first, True)])
        self.client.force_authenticate(self.reader)
        with CaptureQueriesContext(connection) as baseline:
            self.client.get(reverse("journey-list"), format="json")

        for index in range(1, 5):
            story = self._story(f"query-story-{index}")
            self._journey(f"query-path-{index}", [(story, True)])

        with self.assertNumQueries(len(baseline.captured_queries)):
            self.client.get(reverse("journey-list"), format="json")


class StoryReactionApiTests(APITestCase):
    """A reaction is one tap at the end of a story — separate from Review,
    which stays untouched (§10.1)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="reactor@example.com", username="reactor", password="test-password"
        )
        self.other = User.objects.create_user(
            email="reactor2@example.com", username="reactor2", password="test-password"
        )
        self.story = Story.objects.create(title="A Tale", slug="a-reacted-tale", is_published=True)

    def _url(self):
        return reverse("story-reactions", kwargs={"slug": self.story.slug})

    def _react(self, reaction_type):
        return self.client.post(self._url(), {"reaction_type": reaction_type}, format="json")

    def test_anyone_can_see_the_totals(self):
        """§10.4 — a panel showing how readers felt is worth seeing before you
        have an account."""
        self.client.force_authenticate(self.user)
        self._react(StoryReaction.LOVED)
        self.client.force_authenticate(None)

        response = self.client.get(self._url(), format="json")

        self.assertEqual(response.status_code, 200)
        counts = {row["type"]: row["count"] for row in response.data["reactions"]}
        self.assertEqual(counts[StoryReaction.LOVED], 1)
        self.assertIsNone(response.data["my_reaction"])

    def test_every_reaction_type_is_returned_even_at_zero(self):
        """Otherwise a story would offer fewer ways to answer the longer it
        went unreacted-to."""
        response = self.client.get(self._url(), format="json")

        self.assertEqual(len(response.data["reactions"]), len(StoryReaction.REACTION_CHOICES))
        self.assertEqual(response.data["total"], 0)

    def test_reacting_requires_an_account(self):
        response = self._react(StoryReaction.LOVED)

        self.assertEqual(response.status_code, 401)
        self.assertFalse(StoryReaction.objects.exists())

    def test_a_reader_has_one_reaction_per_story(self):
        self.client.force_authenticate(self.user)

        self._react(StoryReaction.LOVED)
        response = self._react(StoryReaction.FUNNY)

        self.assertEqual(StoryReaction.objects.filter(user=self.user).count(), 1)
        self.assertEqual(response.data["my_reaction"], StoryReaction.FUNNY)
        counts = {row["type"]: row["count"] for row in response.data["reactions"]}
        self.assertEqual(counts[StoryReaction.LOVED], 0)
        self.assertEqual(counts[StoryReaction.FUNNY], 1)

    def test_tapping_the_same_reaction_again_removes_it(self):
        """The tap that set it is the tap that unsets it."""
        self.client.force_authenticate(self.user)
        self._react(StoryReaction.LOVED)

        response = self._react(StoryReaction.LOVED)

        self.assertIsNone(response.data["my_reaction"])
        self.assertEqual(response.data["total"], 0)

    def test_a_reaction_can_be_deleted_outright(self):
        self.client.force_authenticate(self.user)
        self._react(StoryReaction.LOVED)

        response = self.client.delete(self._url(), format="json")

        self.assertIsNone(response.data["my_reaction"])
        self.assertFalse(StoryReaction.objects.exists())

    def test_readers_do_not_overwrite_each_other(self):
        self.client.force_authenticate(self.user)
        self._react(StoryReaction.LOVED)
        self.client.force_authenticate(self.other)
        self._react(StoryReaction.LOVED)

        response = self.client.get(self._url(), format="json")

        counts = {row["type"]: row["count"] for row in response.data["reactions"]}
        self.assertEqual(counts[StoryReaction.LOVED], 2)

    def test_an_unknown_reaction_is_rejected(self):
        self.client.force_authenticate(self.user)

        response = self._react("shrug")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StoryReaction.objects.exists())

    def test_changing_a_reaction_records_what_it_replaced(self):
        self.client.force_authenticate(self.user)
        self._react(StoryReaction.LOVED)

        self._react(StoryReaction.EMOTIONAL)

        events = AnalyticsEvent.objects.filter(
            event_type=AnalyticsEvent.EVENT_REACTION_ADDED
        ).order_by("created_at")
        self.assertEqual(events.count(), 2)
        self.assertIsNone(events[0].metadata["previous"])
        self.assertEqual(events[1].metadata["previous"], StoryReaction.LOVED)

    def test_removing_a_reaction_raises_no_event(self):
        self.client.force_authenticate(self.user)
        self._react(StoryReaction.LOVED)

        self._react(StoryReaction.LOVED)

        self.assertEqual(
            AnalyticsEvent.objects.filter(
                event_type=AnalyticsEvent.EVENT_REACTION_ADDED
            ).count(),
            1,
        )

    def test_reactions_leave_reviews_alone(self):
        """§10.1 — the two systems are independent."""
        Review.objects.create(user=self.user, story=self.story, rating=5, comment="Lovely.")
        self.client.force_authenticate(self.user)

        self._react(StoryReaction.LOVED)

        self.assertEqual(Review.objects.filter(user=self.user, story=self.story).count(), 1)
        self.assertEqual(
            Review.objects.get(user=self.user, story=self.story).rating, 5
        )


class HeroTemplateTests(APITestCase):
    """Backend-controlled homepage hero: which template shows, and how the
    admin panel manages them."""

    def setUp(self):
        cache.clear()
        # The migration seeds a "Regular" default; start from a known state.
        HeroTemplate.objects.all().delete()
        self.admin = User.objects.create_user(
            email="heroadmin@example.com", username="heroadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="heroreader@example.com", username="heroreader", password="test-password"
        )
        self.regular = HeroTemplate.objects.create(name="Regular", is_default=True)
        self.now = timezone.now()

    def _scheduled(self, name, start_days, end_days):
        return HeroTemplate.objects.create(
            name=name,
            starts_at=self.now + timedelta(days=start_days),
            ends_at=self.now + timedelta(days=end_days),
        )

    def test_default_shows_outside_any_schedule(self):
        self._scheduled("Later", 5, 10)
        self._scheduled("Earlier", -10, -5)
        self.assertEqual(HeroTemplate.objects.current(self.now), self.regular)

    def test_scheduled_template_overrides_default_inside_its_window(self):
        halloween = self._scheduled("Halloween", -1, 1)
        self.assertEqual(HeroTemplate.objects.current(self.now), halloween)

    def test_current_is_none_without_default_or_live_schedule(self):
        self.regular.delete()
        self.assertIsNone(HeroTemplate.objects.current(self.now))

    def test_only_one_default_is_allowed(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            HeroTemplate.objects.create(name="Second", is_default=True)

    def test_home_payload_resolves_live_and_custom_stats(self):
        self.regular.stat_2_source = HeroTemplate.STAT_CUSTOM
        self.regular.stat_2_value = "13"
        self.regular.stat_2_label = "spooky tales"
        self.regular.stat_3_label = ""
        self.regular.info_line_2_text = ""
        self.regular.save()
        Story.objects.create(title="Live", slug="hero-live", is_published=True)

        hero = self.client.get(reverse("home-data")).json()["hero"]

        self.assertEqual(
            hero["stats"],
            [{"label": "stories", "value": 1}, {"label": "spooky tales", "value": "13"}],
        )
        self.assertEqual(hero["info_lines"], [])
        self.assertEqual(hero["cta"]["url"], "/library")
        self.assertEqual(hero["background"]["animation"], "classic")

    def test_row_switches_hide_whole_rows_but_keep_their_content(self):
        self.regular.info_line_1_text = "Full novels"
        self.regular.show_info_lines = False
        self.regular.show_stats = False
        self.regular.save()

        hero = self.client.get(reverse("home-data")).json()["hero"]

        self.assertEqual(hero["info_lines"], [])
        self.assertEqual(hero["stats"], [])
        self.regular.refresh_from_db()
        self.assertEqual(self.regular.info_line_1_text, "Full novels")

    def test_home_payload_hero_is_null_when_nothing_applies(self):
        self.regular.delete()
        self.assertIsNone(self.client.get(reverse("home-data")).json()["hero"])

    def test_admin_api_requires_superuser(self):
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(reverse("admin-hero-template-list")).status_code, 403)

    def test_admin_lists_with_status(self):
        self.client.force_authenticate(self.admin)
        self._scheduled("Later", 5, 10)
        rows = {row["name"]: row["status"] for row in self.client.get(reverse("admin-hero-template-list")).json()}
        self.assertEqual(rows, {"Regular": "live", "Later": "scheduled"})

    def test_admin_rejects_overlapping_schedule(self):
        self.client.force_authenticate(self.admin)
        self._scheduled("Halloween", 1, 5)
        response = self.client.post(
            reverse("admin-hero-template-list"),
            {
                "name": "Clash",
                "starts_at": (self.now + timedelta(days=3)).isoformat(),
                "ends_at": (self.now + timedelta(days=8)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("starts_at", response.json())

    def test_admin_rejects_half_a_schedule_and_unsafe_urls(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-hero-template-detail", kwargs={"pk": self.regular.pk})
        half = self.client.patch(url, {"starts_at": self.now.isoformat()}, format="json")
        self.assertIn("ends_at", half.json())
        for bad in ["javascript:alert(1)", "//evil.example", "library"]:
            response = self.client.patch(url, {"cta_url": bad}, format="json")
            self.assertEqual(response.status_code, 400, bad)
        bad_icon = self.client.patch(url, {"animation_icons": ["NotAnIcon"]}, format="json")
        self.assertEqual(bad_icon.status_code, 400)

    def test_admin_cannot_set_default_through_a_plain_save(self):
        self.client.force_authenticate(self.admin)
        other = HeroTemplate.objects.create(name="Other")
        self.client.patch(
            reverse("admin-hero-template-detail", kwargs={"pk": other.pk}),
            {"is_default": True},
            format="json",
        )
        other.refresh_from_db()
        self.assertFalse(other.is_default)

    def test_set_default_moves_the_flag(self):
        self.client.force_authenticate(self.admin)
        other = HeroTemplate.objects.create(name="Other")
        response = self.client.post(reverse("admin-hero-template-set-default", kwargs={"pk": other.pk}))
        self.assertEqual(response.status_code, 200)
        self.regular.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(self.regular.is_default)
        self.assertTrue(other.is_default)

    def test_duplicate_is_neither_default_nor_scheduled(self):
        self.client.force_authenticate(self.admin)
        self.regular.starts_at = self.now - timedelta(days=1)
        self.regular.ends_at = self.now + timedelta(days=1)
        self.regular.cta_label = "Read now"
        self.regular.save()
        response = self.client.post(reverse("admin-hero-template-duplicate", kwargs={"pk": self.regular.pk}))
        self.assertEqual(response.status_code, 201)
        copy = HeroTemplate.objects.get(pk=response.json()["id"])
        self.assertEqual(copy.name, "Regular (copy)")
        self.assertEqual(copy.cta_label, "Read now")
        self.assertFalse(copy.is_default)
        self.assertIsNone(copy.starts_at)


class HeroTitleSpacingTests(APITestCase):
    def test_title_prefix_keeps_its_trailing_space(self):
        HeroTemplate.objects.all().delete()
        admin = User.objects.create_user(
            email="herospace@example.com", username="herospace",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.client.force_authenticate(admin)
        response = self.client.post(
            reverse("admin-hero-template-list"),
            {"name": "Halloween", "title_prefix": "Spooky ", "title_highlight": " Stories "},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["title_prefix"], "Spooky ")
        self.assertEqual(response.json()["title_highlight"], "Stories")


class HeroTypographyTests(APITestCase):
    def setUp(self):
        cache.clear()
        HeroTemplate.objects.all().delete()
        self.admin = User.objects.create_user(
            email="herotype@example.com", username="herotype",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.template = HeroTemplate.objects.create(name="Regular", is_default=True)

    def test_defaults_reproduce_the_original_hero(self):
        typography = self.client.get(reverse("home-data")).json()["hero"]["typography"]
        self.assertEqual(
            typography,
            {
                "title_font": "", "title_size": "medium", "title_weight": 700,
                "title_letter_spacing": "tight", "title_uppercase": False, "title_italic": False,
                "body_font": "", "description_size": "medium", "cta_uppercase": False,
            },
        )

    def test_admin_sets_typography(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-hero-template-detail", kwargs={"pk": self.template.pk})
        response = self.client.patch(
            url,
            {"title_font": "Creepster", "title_size": "xlarge", "title_weight": 400, "title_italic": True, "body_font": "Lora"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        cache.clear()
        typography = self.client.get(reverse("home-data")).json()["hero"]["typography"]
        self.assertEqual((typography["title_font"], typography["title_size"], typography["body_font"]), ("Creepster", "xlarge", "Lora"))

    def test_admin_rejects_unknown_fonts_and_bad_values(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-hero-template-detail", kwargs={"pk": self.template.pk})
        for payload in ({"title_font": "Comic Sans"}, {"title_weight": 1000}, {"title_size": "huge"}, {"description_size": "tiny"}):
            self.assertEqual(self.client.patch(url, payload, format="json").status_code, 400, payload)


class HomeSectionTests(APITestCase):
    UA = {"HTTP_USER_AGENT": "Mozilla/5.0 Chrome"}

    def setUp(self):
        from apps.story.models import HomeSection

        self.HomeSection = HomeSection
        HomeSection.objects.all().delete()  # drop the migration's seed rows
        self.admin = User.objects.create(
            email="homeadmin@example.com", username="homeadmin", is_superuser=True, is_staff=True, is_active=True
        )
        for n in range(3):
            Story.objects.create(title=f"Home Story {n}", slug=f"home-story-{n}", is_published=True, views=n)

    def _public(self):
        cache.clear()
        return self.client.get("/api/home/", **self.UA).json()["sections"]

    def test_admin_endpoints_require_superuser(self):
        self.assertEqual(self.client.get("/api/admin/home-sections/").status_code, 401)

    def test_disabled_and_empty_sections_are_not_served(self):
        self.HomeSection.objects.create(
            section_type="stories_query", title="Shown", position=0, config={"sort": "popular"}
        )
        self.HomeSection.objects.create(
            section_type="stories_query", title="Hidden", position=1, is_enabled=False
        )
        self.HomeSection.objects.create(section_type="videos", title="No videos", position=2)
        self.HomeSection.objects.create(section_type="blogs", title="No blogs", position=3)
        sections = self._public()
        self.assertEqual([s["title"] for s in sections], ["Shown"])
        # popular = most views first
        self.assertEqual(sections[0]["items"][0]["title"], "Home Story 2")

    def test_order_follows_position_and_manual_picks_keep_their_order(self):
        ids = list(Story.objects.order_by("id").values_list("id", flat=True))
        self.HomeSection.objects.create(
            section_type="stories_manual", title="Picks", position=5, config={"story_ids": [ids[2], ids[0]]}
        )
        self.HomeSection.objects.create(section_type="banner", title="Banner", position=1)
        sections = self._public()
        self.assertEqual([s["title"] for s in sections], ["Banner", "Picks"])
        self.assertEqual([i["id"] for i in sections[1]["items"]], [ids[2], ids[0]])

    def test_admin_create_validates_config_and_reorder(self):
        self.client.force_authenticate(self.admin)
        bad = self.client.post(
            "/api/admin/home-sections/",
            {"title": "x", "section_type": "stories_query", "config": {"sort": "nope"}},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)
        first = self.client.post(
            "/api/admin/home-sections/",
            {"title": "A", "section_type": "stories_query", "config": {"sort": "popular", "junk": 1}},
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        self.assertNotIn("junk", first.json()["config"])
        second = self.client.post(
            "/api/admin/home-sections/", {"title": "B", "section_type": "videos"}, format="json"
        )
        a, b = first.json()["id"], second.json()["id"]
        self.assertLess(first.json()["position"], second.json()["position"])
        self.assertEqual(self.client.post("/api/admin/home-sections/reorder/", {"ids": [a]}, format="json").status_code, 400)
        reordered = self.client.post("/api/admin/home-sections/reorder/", {"ids": [b, a]}, format="json")
        self.assertEqual([s["id"] for s in reordered.json()], [b, a])

    def test_toggle_and_duplicate(self):
        self.client.force_authenticate(self.admin)
        section = self.HomeSection.objects.create(section_type="stories_query", title="T", position=0)
        self.assertFalse(self.client.post(f"/api/admin/home-sections/{section.pk}/toggle/").json()["is_enabled"])
        copy = self.client.post(f"/api/admin/home-sections/{section.pk}/duplicate/").json()
        self.assertFalse(copy["is_enabled"])
        self.assertEqual(copy["position"], 1)

    def test_layout_and_card_style_matrix_validation_types_and_home_response(self):
        from apps.story.home_sections import (
            MEDIA_CARD_STYLES,
            MEDIA_LAYOUTS,
            STORY_CARD_STYLES,
            STORY_LAYOUTS,
        )

        self.client.force_authenticate(self.admin)

        # 1. types endpoint returns the exact matrix per type
        types_resp = self.client.get("/api/admin/home-sections/types/")
        self.assertEqual(types_resp.status_code, 200)
        types_by_val = {entry["value"]: entry for entry in types_resp.json()["types"]}
        for stype in ("stories_query", "stories_manual"):
            self.assertEqual([x["value"] for x in types_by_val[stype]["layouts"]], STORY_LAYOUTS)
            self.assertEqual([x["value"] for x in types_by_val[stype]["card_styles"]], STORY_CARD_STYLES)
            for opt in types_by_val[stype]["layouts"]:
                self.assertTrue({"value", "label", "description"} <= set(opt.keys()))
        for stype in ("videos", "blogs"):
            self.assertEqual([x["value"] for x in types_by_val[stype]["layouts"]], MEDIA_LAYOUTS)
            self.assertEqual([x["value"] for x in types_by_val[stype]["card_styles"]], MEDIA_CARD_STYLES)

        # 2. Every allowed combination saves
        for stype, layouts, styles in (
            ("stories_query", STORY_LAYOUTS, STORY_CARD_STYLES),
            ("stories_manual", STORY_LAYOUTS, STORY_CARD_STYLES),
            ("videos", MEDIA_LAYOUTS, MEDIA_CARD_STYLES),
            ("blogs", MEDIA_LAYOUTS, MEDIA_CARD_STYLES),
        ):
            for layout in layouts:
                resp = self.client.post(
                    "/api/admin/home-sections/",
                    {"title": f"{stype}-{layout}", "section_type": stype, "layout": layout},
                    format="json",
                )
                self.assertEqual(resp.status_code, 201, msg=(stype, layout, resp.content))
            for style in styles:
                resp = self.client.post(
                    "/api/admin/home-sections/",
                    {"title": f"{stype}-{style}", "section_type": stype, "card_style": style},
                    format="json",
                )
                self.assertEqual(resp.status_code, 201, msg=(stype, style, resp.content))

        # 3. Every disallowed combination is rejected with 400
        for stype in ("videos", "blogs"):
            for bad_layout in ("trending", "covers"):
                resp = self.client.post(
                    "/api/admin/home-sections/",
                    {"title": "Bad", "section_type": stype, "layout": bad_layout},
                    format="json",
                )
                self.assertEqual(resp.status_code, 400, msg=(stype, bad_layout))
                self.assertIn("layout", resp.json())
            bad_style_resp = self.client.post(
                "/api/admin/home-sections/",
                {"title": "Bad style", "section_type": stype, "card_style": "compact"},
                format="json",
            )
            self.assertEqual(bad_style_resp.status_code, 400, msg=(stype, "compact"))
            self.assertIn("card_style", bad_style_resp.json())

        # 4. /api/home/ returns new layout & card_style unchanged
        self.HomeSection.objects.all().delete()
        self.HomeSection.objects.create(
            section_type="stories_query",
            title="Bento Overlay",
            position=0,
            layout="bento",
            card_style="overlay",
        )
        sections = self._public()
        self.assertEqual(sections[0]["layout"], "bento")
        self.assertEqual(sections[0]["card_style"], "overlay")

    def test_display_anchor_device_summary_duplicate_and_preview_parity(self):
        from apps.story.home_sections import DEFAULT_DISPLAY, clean_display

        # clean_display defaults and validation errors
        self.assertEqual(clean_display({}), DEFAULT_DISPLAY)
        self.assertEqual(clean_display({"unknown_key": 123, "background": "dark"})["background"], "dark")
        self.assertNotIn("unknown_key", clean_display({"unknown_key": 123}))
        for bad in (
            {"background": "neon"},
            {"heading_align": "right"},
            {"spacing": "huge"},
            {"width": "wide"},
            {"hide_title": "yes"},
            {"show_summary": 1},
        ):
            with self.assertRaises(ValueError):
                clean_display(bad)

        self.client.force_authenticate(self.admin)

        # anchor_id validation: uppercase, reserved, duplicate
        for bad_anchor in ("Upper", "1starts-digit", "main", "header", "bad space"):
            resp = self.client.post(
                "/api/admin/home-sections/",
                {"title": "A", "section_type": "stories_query", "anchor_id": bad_anchor},
                format="json",
            )
            self.assertEqual(resp.status_code, 400, msg=bad_anchor)
            self.assertIn("anchor_id", resp.json())

        # device validation
        bad_dev = self.client.post(
            "/api/admin/home-sections/",
            {"title": "A", "section_type": "stories_query", "device": "tablet"},
            format="json",
        )
        self.assertEqual(bad_dev.status_code, 400)
        self.assertIn("device", bad_dev.json())

        # Create valid section with anchor_id, device, display
        Story.objects.filter(slug="home-story-0").update(
            summary="<p>" + ("word " * 50).strip() + "</p>"
        )
        created = self.client.post(
            "/api/admin/home-sections/",
            {
                "title": "Featured Band",
                "section_type": "stories_query",
                "anchor_id": "featured-band",
                "device": "desktop",
                "display": {"background": "tint", "show_summary": True, "show_rating": True},
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        section_id = created.json()["id"]

        # Duplicate anchor_id rejected
        dup_anchor = self.client.post(
            "/api/admin/home-sections/",
            {"title": "B", "section_type": "stories_query", "anchor_id": "featured-band"},
            format="json",
        )
        self.assertEqual(dup_anchor.status_code, 400)
        self.assertIn("anchor_id", dup_anchor.json())

        # Duplicate action clears anchor_id but keeps device and display
        copy = self.client.post(f"/api/admin/home-sections/{section_id}/duplicate/").json()
        self.assertEqual(copy["anchor_id"], "")
        self.assertEqual(copy["device"], "desktop")
        self.assertEqual(copy["display"]["background"], "tint")
        self.assertTrue(copy["display"]["show_summary"])

        # Old section with empty display returns full defaults and NO summary on stories
        old = self.HomeSection.objects.create(
            section_type="stories_query", title="Legacy", position=10, display={}
        )
        sections = self._public()
        by_id = {s["id"]: s for s in sections}
        self.assertEqual(by_id[old.pk]["display"], DEFAULT_DISPLAY)
        self.assertEqual(by_id[old.pk]["device"], "all")
        self.assertEqual(by_id[old.pk]["anchor_id"], "")
        self.assertNotIn("summary", by_id[old.pk]["items"][0])

        # Section with show_summary=True carries trimmed summary <= 160 chars
        self.assertEqual(by_id[section_id]["device"], "desktop")
        self.assertEqual(by_id[section_id]["anchor_id"], "featured-band")
        self.assertTrue(by_id[section_id]["display"]["show_summary"])
        with_summary = next(i for i in by_id[section_id]["items"] if i["slug"] == "home-story-0")
        self.assertIn("summary", with_summary)
        self.assertLessEqual(len(with_summary["summary"]), 161)

        # Preview parity with display.show_summary
        prev_on = self.client.post(
            "/api/admin/home-sections/preview/",
            {"section_type": "stories_query", "display": {"show_summary": True}},
            format="json",
        )
        self.assertEqual(prev_on.status_code, 200)
        self.assertIn("summary", prev_on.json()["items"][0])

        prev_off = self.client.post(
            "/api/admin/home-sections/preview/",
            {"section_type": "stories_query", "display": {"show_summary": False}},
            format="json",
        )
        self.assertEqual(prev_off.status_code, 200)
        self.assertNotIn("summary", prev_off.json()["items"][0])

    def test_templates_list_and_from_template_endpoint(self):
        # Superuser only
        regular = User.objects.create(
            email="nonadmin@example.com", username="nonadmin_tpl", is_superuser=False, is_staff=True, is_active=True
        )
        self.client.force_authenticate(regular)
        self.assertEqual(self.client.get("/api/admin/home-sections/templates/").status_code, 403)
        self.assertEqual(
            self.client.post("/api/admin/home-sections/from-template/", {"key": "new-this-week"}, format="json").status_code,
            403,
        )

        self.client.force_authenticate(self.admin)
        existing = self.HomeSection.objects.create(section_type="banner", title="Existing", position=5)

        list_resp = self.client.get("/api/admin/home-sections/templates/")
        self.assertEqual(list_resp.status_code, 200)
        templates = list_resp.json()
        self.assertEqual(len(templates), 10)
        for tpl in templates:
            self.assertTrue(
                {"key", "name", "description", "section_type", "layout", "card_style", "summary"} <= set(tpl.keys())
            )

        # Unknown key -> 404
        missing = self.client.post(
            "/api/admin/home-sections/from-template/", {"key": "does-not-exist"}, format="json"
        )
        self.assertEqual(missing.status_code, 404)

        # Creating from every template succeeds, hidden, appended at end
        expected_pos = 6
        for tpl in templates:
            resp = self.client.post(
                "/api/admin/home-sections/from-template/", {"key": tpl["key"]}, format="json"
            )
            self.assertEqual(resp.status_code, 201, msg=(tpl["key"], resp.content))
            body = resp.json()
            self.assertFalse(body["is_enabled"])
            self.assertEqual(body["position"], expected_pos)
            expected_pos += 1

        existing.refresh_from_db()
        self.assertEqual(existing.position, 5)


class HomeSectionStoryRulesTests(APITestCase):
    def setUp(self):
        from apps.story.models import HomeSection

        cache.clear()
        self.HomeSection = HomeSection
        HomeSection.objects.all().delete()
        self.today = timezone.localdate()
        self.author_a = Author.objects.create(name="Author A", slug="author-a-rules")
        self.author_b = Author.objects.create(name="Author B", slug="author-b-rules")
        self.a = self._story(
            "Zulu", "rules-zulu", country="NP", author=self.author_a,
            site_published_date=self.today - timedelta(days=10), views=5, rating=4,
            cached_chapter_reading_minutes=3, cached_file_reading_minutes=99,
        )
        self.b = self._story(
            "Alpha", "rules-alpha", country="FR", author=self.author_b,
            site_published_date=self.today - timedelta(days=5), views=2, rating=5,
            cached_file_reading_minutes=10,
        )
        self.c = self._story(
            "Middle", "rules-middle", country="NP", author=self.author_b,
            site_published_date=self.today - timedelta(days=1), views=100, rating=0,
        )
        self.reader_a = User.objects.create(
            email="rules-reader-a@example.com", username="rules-reader-a", is_active=True
        )
        self.reader_b = User.objects.create(
            email="rules-reader-b@example.com", username="rules-reader-b", is_active=True
        )

    def _story(self, title, slug, **extra):
        return Story.objects.create(title=title, slug=slug, is_published=True, **extra)

    def _section(self, **config):
        from apps.story.home_sections import clean_config

        return self.HomeSection.objects.create(
            section_type="stories_query",
            title=f"Rules {self.HomeSection.objects.count()}",
            item_limit=24,
            allow_repeats=True,
            config=clean_config("stories_query", config),
        )

    def _ids(self, section):
        from apps.story.home_sections import story_queryset

        return [story.pk for story in story_queryset(section)]

    def test_new_database_sorts_and_null_reading_times_last(self):
        self.assertEqual(self._ids(self._section(sort="oldest")), [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual(self._ids(self._section(sort="az")), [self.b.pk, self.c.pk, self.a.pk])
        # Chapter time is the card value and therefore wins over the file cache.
        self.assertEqual(self._ids(self._section(sort="shortest")), [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual(self._ids(self._section(sort="longest")), [self.b.pk, self.a.pk, self.c.pk])

    def test_most_completed_and_hidden_gems_order(self):
        StoryCompletion.objects.create(user=self.reader_a, story=self.a, source="chapters")
        StoryCompletion.objects.create(user=self.reader_a, story=self.b, source="chapters")
        StoryCompletion.objects.create(user=self.reader_b, story=self.b, source="chapters")
        self.assertEqual(self._ids(self._section(sort="most_completed"))[:3], [self.b.pk, self.a.pk, self.c.pk])
        # Published views are [2, 5, 100], so the median is 5. Zero-rated C is excluded.
        self.assertEqual(self._ids(self._section(sort="hidden_gems")), [self.b.pk, self.a.pk])

    def test_daily_shuffle_is_stable_per_day_and_differs_by_section(self):
        for number in range(8):
            self._story(f"Shuffle {number}", f"shuffle-{number}")
        first = self._section(sort="daily_shuffle")
        second = self._section(sort="daily_shuffle")
        first_order = self._ids(first)
        self.assertEqual(self._ids(first), first_order)
        self.assertNotEqual(self._ids(second), first_order)

    def test_country_author_and_rule_filters(self):
        Review.objects.create(story=self.a, user=self.reader_a, rating=4)
        Review.objects.create(story=self.b, user=self.reader_a, rating=5)
        Review.objects.create(story=self.b, user=self.reader_b, rating=5)
        self.assertEqual(self._ids(self._section(countries=["np"], authors=[self.author_a.pk])), [self.a.pk])
        self.assertEqual(self._ids(self._section(min_reading_minutes=5, max_reading_minutes=12)), [self.b.pk])
        self.assertEqual(
            set(self._ids(self._section(published_within_days=7))), {self.b.pk, self.c.pk}
        )
        self.assertEqual(self._ids(self._section(min_rating=4.5)), [self.b.pk])
        self.assertEqual(self._ids(self._section(min_ratings_count=2)), [self.b.pk])

    def test_exclusions_pins_and_no_repeat(self):
        excluded_tag = Tag.objects.create(name="Excluded", slug="excluded-rules")
        self.a.tags.add(excluded_tag)
        pinned = self._section(countries=["NP"], pin_story_ids=[self.b.pk, self.a.pk, self.b.pk])
        self.assertEqual(self._ids(pinned), [self.b.pk, self.a.pk, self.c.pk])
        self.assertEqual(self._ids(pinned).count(self.b.pk), 1)

        section = self._section(
            countries=["NP"],
            exclude_tags=[excluded_tag.pk],
            exclude_story_ids=[self.c.pk],
            pin_story_ids=[self.b.pk, self.a.pk, self.b.pk],
        )
        # B ignores the country include filter. A and C are removed by explicit
        # exclusions, which take precedence over pins/includes.
        self.assertEqual(self._ids(section), [self.b.pk])

    def test_trending_window_is_forwarded(self):
        with patch("apps.story.home_metrics.trending_story_ids", return_value=[self.b.pk]) as trending:
            self.assertEqual(self._ids(self._section(sort="trending", trending_days=30)), [self.b.pk])
        trending.assert_called_once_with(30)

    def test_clean_config_validation_and_unknown_keys(self):
        from apps.story.home_sections import clean_config

        cleaned = clean_config("stories_query", {"countries": ["np"], "unknown": True})
        self.assertEqual(cleaned["countries"], ["NP"])
        self.assertNotIn("unknown", cleaned)
        invalid = [
            {"countries": ["XX"]},
            {"countries": ["NP"] * 31},
            {"authors": "1"},
            {"exclude_story_ids": list(range(51))},
            {"pin_story_ids": list(range(13))},
            {"min_reading_minutes": 0},
            {"max_reading_minutes": 601},
            {"min_reading_minutes": 20, "max_reading_minutes": 10},
            {"published_within_days": 0},
            {"min_rating": 5.1},
            {"min_ratings_count": 1001},
            {"trending_days": 14},
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, r".+"):
                clean_config("stories_query", config)

    def test_old_style_config_keeps_the_old_order(self):
        section = self.HomeSection.objects.create(
            section_type="stories_query", title="Legacy", config={"sort": "popular"}
        )
        self.assertEqual(self._ids(section), [self.c.pk, self.a.pk, self.b.pk])

    def test_story_queryset_query_count_is_constant_as_catalogue_grows(self):
        from apps.story.home_sections import story_queryset

        section = self._section(sort="newest")
        with CaptureQueriesContext(connection) as small:
            list(story_queryset(section))
        for number in range(20):
            self._story(f"Extra {number}", f"rules-extra-{number}")
        with CaptureQueriesContext(connection) as large:
            list(story_queryset(section))
        self.assertEqual(len(large), len(small))

    def test_types_endpoint_exposes_described_sorts_by_content_type(self):
        admin = User.objects.create(
            email="rules-admin@example.com", username="rules-admin",
            is_superuser=True, is_staff=True, is_active=True,
        )
        self.client.force_authenticate(admin)
        sorts = self.client.get("/api/admin/home-sections/types/").json()["sorts"]
        self.assertEqual(set(sorts), {"stories", "videos", "blogs"})
        story_options = {option["value"]: option for option in sorts["stories"]}
        self.assertEqual(set(story_options), {
            "newest", "recently_added", "popular", "top_rated", "trending",
            "oldest", "az", "shortest", "longest", "most_completed",
            "hidden_gems", "daily_shuffle",
        })
        self.assertTrue(all(option["label"] and option["description"] for option in story_options.values()))


class HomeSectionVideoBlogRulesTests(APITestCase):
    def setUp(self):
        from apps.story.models import HomeSection

        cache.clear()
        self.HomeSection = HomeSection
        HomeSection.objects.all().delete()
        self.now = timezone.now()
        self.genre = Genre.objects.create(name="Rules genre", slug="rules-genre")
        self.story_en = Story.objects.create(
            title="English story", slug="video-rules-en", language="en", is_published=True
        )
        self.story_en.genres.add(self.genre)
        self.story_fr = Story.objects.create(
            title="French story", slug="video-rules-fr", language="fr", is_published=True
        )
        self.draft_story = Story.objects.create(
            title="Draft story", slug="blog-rules-draft", is_published=False
        )
        self.video_type = VideoType.objects.create(name="Rules type")
        self.video_old = self._video(
            "Old landscape", "video-old", "oldvideo001", story=self.story_en,
            duration_seconds=300, aspect_ratio="16:9", video_type=self.video_type,
        )
        self.video_new = self._video(
            "New vertical", "video-new", "newvideo001", story=self.story_fr,
            duration_seconds=60, aspect_ratio="9:16",
        )
        self.video_null = self._video(
            "Standalone", "video-null", "nullvideo01", duration_seconds=None,
        )
        Video.objects.filter(pk=self.video_old.pk).update(created_at=self.now - timedelta(days=3))
        Video.objects.filter(pk=self.video_new.pk).update(created_at=self.now - timedelta(days=2))
        Video.objects.filter(pk=self.video_null.pk).update(created_at=self.now - timedelta(days=1))

        self.topic = BlogTopic.objects.create(name="Rules topic", slug="rules-topic")
        self.blog_old = Blog.objects.create(
            title="Old post", slug="rules-old-post", content="word " * 230
        )
        self.blog_new = Blog.objects.create(
            title="New post", slug="rules-new-post", content="word " * 690
        )
        self.blog_null = Blog.objects.create(
            title="Null post", slug="rules-null-post", content="word"
        )
        Blog.objects.filter(pk=self.blog_old.pk).update(created_at=self.now - timedelta(days=10))
        Blog.objects.filter(pk=self.blog_new.pk).update(created_at=self.now - timedelta(days=2))
        Blog.objects.filter(pk=self.blog_null.pk).update(
            created_at=self.now - timedelta(days=1), reading_time_minutes=None
        )
        self.blog_old.topics.add(self.topic)
        self.blog_old.linked_stories.add(self.story_en)
        self.blog_new.linked_stories.add(self.draft_story)

    def _video(self, title, slug, youtube_id, **extra):
        return Video.objects.create(
            title=title, slug=slug, youtube_id=youtube_id,
            youtube_url=f"https://youtu.be/{youtube_id}", **extra,
        )

    def _section(self, section_type, **config):
        from apps.story.home_sections import clean_config

        return self.HomeSection.objects.create(
            section_type=section_type,
            title=f"Rules {self.HomeSection.objects.count()}",
            item_limit=24,
            config=clean_config(section_type, config),
        )

    def _video_ids(self, section):
        from apps.story.home_sections import video_queryset

        return [video.pk for video in video_queryset(section)]

    def _blog_ids(self, section):
        from apps.story.home_sections import blog_queryset

        return [blog.pk for blog in blog_queryset(section)]

    def test_video_sorts_keep_null_durations_last(self):
        self.assertEqual(
            self._video_ids(self._section("videos", video_sort="newest")),
            [self.video_null.pk, self.video_new.pk, self.video_old.pk],
        )
        self.assertEqual(
            self._video_ids(self._section("videos", video_sort="oldest")),
            [self.video_old.pk, self.video_new.pk, self.video_null.pk],
        )
        self.assertEqual(
            self._video_ids(self._section("videos", video_sort="shortest")),
            [self.video_new.pk, self.video_old.pk, self.video_null.pk],
        )
        self.assertEqual(
            self._video_ids(self._section("videos", video_sort="longest")),
            [self.video_old.pk, self.video_new.pk, self.video_null.pk],
        )

    def test_video_filters_and_existing_filters(self):
        self.assertEqual(
            self._video_ids(self._section("videos", format="vertical")), [self.video_new.pk]
        )
        self.assertEqual(
            self._video_ids(
                self._section("videos", min_duration_seconds=50, max_duration_seconds=100)
            ),
            [self.video_new.pk],
        )
        # Standalone videos have no language, so the language setting is skipped for them.
        self.assertEqual(
            set(self._video_ids(self._section("videos", language="en"))),
            {self.video_old.pk, self.video_null.pk},
        )
        self.assertEqual(
            self._video_ids(self._section("videos", story_genres=[self.genre.pk])),
            [self.video_old.pk],
        )
        self.assertEqual(
            self._video_ids(self._section("videos", video_type=self.video_type.pk)),
            [self.video_old.pk],
        )
        self.assertEqual(
            self._video_ids(self._section("videos", standalone_only=True)),
            [self.video_null.pk],
        )

    def test_video_hand_pick_overrides_filters_and_keeps_order(self):
        section = self._section(
            "videos", video_ids=[self.video_old.pk, self.video_new.pk],
            format="vertical", min_duration_seconds=9999, standalone_only=True,
        )
        self.assertEqual(self._video_ids(section), [self.video_old.pk, self.video_new.pk])

    def test_video_daily_shuffle_is_stable(self):
        for number in range(6):
            self._video(f"Shuffle video {number}", f"video-shuffle-{number}", f"shuffle{number:04}")
        section = self._section("videos", video_sort="daily_shuffle")
        self.assertEqual(self._video_ids(section), self._video_ids(section))

    def test_blog_sorts_keep_null_reading_time_last(self):
        self.assertEqual(
            self._blog_ids(self._section("blogs", blog_sort="newest")),
            [self.blog_null.pk, self.blog_new.pk, self.blog_old.pk],
        )
        self.assertEqual(
            self._blog_ids(self._section("blogs", blog_sort="oldest")),
            [self.blog_old.pk, self.blog_new.pk, self.blog_null.pk],
        )
        self.assertEqual(
            self._blog_ids(self._section("blogs", blog_sort="shortest")),
            [self.blog_old.pk, self.blog_new.pk, self.blog_null.pk],
        )

    def test_blog_filters(self):
        self.assertEqual(
            self._blog_ids(self._section("blogs", topics=[self.topic.pk])), [self.blog_old.pk]
        )
        self.assertEqual(
            set(self._blog_ids(self._section("blogs", published_within_days=3))),
            {self.blog_new.pk, self.blog_null.pk},
        )
        self.assertEqual(
            self._blog_ids(self._section("blogs", story_genres=[self.genre.pk])),
            [self.blog_old.pk],
        )
        self.assertEqual(
            self._blog_ids(self._section("blogs", only_with_story=True)), [self.blog_old.pk]
        )

    def test_blog_pins_and_hand_pick_override(self):
        pinned = self._section(
            "blogs", topics=[self.topic.pk], pin_blog_ids=[self.blog_new.pk, self.blog_old.pk]
        )
        self.assertEqual(self._blog_ids(pinned), [self.blog_new.pk, self.blog_old.pk])
        picked = self._section(
            "blogs", blog_ids=[self.blog_null.pk, self.blog_old.pk],
            topics=[999999], only_with_story=True,
        )
        self.assertEqual(self._blog_ids(picked), [self.blog_null.pk, self.blog_old.pk])

    def test_blog_daily_shuffle_is_stable(self):
        for number in range(6):
            Blog.objects.create(
                title=f"Shuffle blog {number}", slug=f"blog-shuffle-{number}", content="word"
            )
        section = self._section("blogs", blog_sort="daily_shuffle")
        self.assertEqual(self._blog_ids(section), self._blog_ids(section))

    def test_video_blog_validation_and_old_config_compatibility(self):
        from apps.story.home_sections import clean_config

        invalid = [
            ("videos", {"video_sort": "most_viewed"}),
            ("videos", {"format": "square"}),
            ("videos", {"min_duration_seconds": -1}),
            ("videos", {"min_duration_seconds": 10, "max_duration_seconds": 5}),
            ("videos", {"video_ids": list(range(25))}),
            ("blogs", {"blog_sort": "most_read"}),
            ("blogs", {"published_within_days": 0}),
            ("blogs", {"pin_blog_ids": list(range(7))}),
            ("blogs", {"blog_ids": list(range(25))}),
        ]
        for section_type, config in invalid:
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, r".+"):
                clean_config(section_type, config)

        legacy_video = self.HomeSection.objects.create(
            section_type="videos", title="Legacy videos", config={}
        )
        legacy_blog = self.HomeSection.objects.create(
            section_type="blogs", title="Legacy blogs", config={}
        )
        self.assertEqual(
            self._video_ids(legacy_video), [self.video_null.pk, self.video_new.pk, self.video_old.pk]
        )
        self.assertEqual(
            self._blog_ids(legacy_blog), [self.blog_null.pk, self.blog_new.pk, self.blog_old.pk]
        )

    def test_video_and_blog_queryset_counts_are_constant(self):
        from apps.story.home_sections import blog_queryset, video_queryset
        from apps.story.serializers import BlogCardSerializer, PublicVideoSerializer

        video_section = self._section("videos", format="landscape")
        blog_section = self._section("blogs", only_with_story=True)
        with CaptureQueriesContext(connection) as small_videos:
            PublicVideoSerializer(video_queryset(video_section), many=True).data
        with CaptureQueriesContext(connection) as small_blogs:
            BlogCardSerializer(blog_queryset(blog_section), many=True).data
        for number in range(10):
            self._video(
                f"Extra video {number}", f"extra-video-{number}", f"extravid{number:03}"
            )
            Blog.objects.create(
                title=f"Extra blog {number}", slug=f"extra-blog-{number}", content="word"
            )
        with CaptureQueriesContext(connection) as large_videos:
            PublicVideoSerializer(video_queryset(video_section), many=True).data
        with CaptureQueriesContext(connection) as large_blogs:
            BlogCardSerializer(blog_queryset(blog_section), many=True).data
        self.assertEqual(len(large_videos), len(small_videos))
        self.assertEqual(len(large_blogs), len(small_blogs))

    def test_types_metadata_lists_supported_video_and_blog_options(self):
        admin = User.objects.create(
            email="video-blog-rules-admin@example.com", username="video-blog-rules-admin",
            is_superuser=True, is_staff=True, is_active=True,
        )
        self.client.force_authenticate(admin)
        data = self.client.get("/api/admin/home-sections/types/").json()
        self.assertEqual(
            [option["value"] for option in data["sorts"]["videos"]],
            ["newest", "oldest", "longest", "shortest", "daily_shuffle"],
        )
        self.assertEqual(
            [option["value"] for option in data["sorts"]["blogs"]],
            ["newest", "oldest", "shortest", "daily_shuffle"],
        )
        by_type = {item["value"]: item for item in data["types"]}
        self.assertIn("video_ids", by_type["videos"]["fields"])
        self.assertIn("blog_ids", by_type["blogs"]["fields"])


class HomeSectionPreviewTests(APITestCase):
    UA = {"HTTP_USER_AGENT": "Mozilla/5.0 Chrome"}

    def setUp(self):
        from apps.story.models import HomeSection

        cache.clear()
        self.HomeSection = HomeSection
        HomeSection.objects.all().delete()
        self.admin = User.objects.create(
            email="preview-admin@example.com", username="preview-admin",
            is_superuser=True, is_staff=True, is_active=True,
        )
        self.regular = User.objects.create(
            email="preview-user@example.com", username="preview-user", is_active=True
        )
        self.stories = [
            Story.objects.create(
                title=f"Preview Story {number}", slug=f"preview-story-{number}",
                is_published=True, views=number,
            )
            for number in range(8)
        ]
        self.video = Video.objects.create(
            title="Preview Video", slug="preview-video", youtube_id="preview00001",
            youtube_url="https://youtu.be/preview00001", duration_seconds=90,
        )
        self.blog = Blog.objects.create(
            title="Preview Blog", slug="preview-blog", content="Preview content"
        )
        self.url = "/api/admin/home-sections/preview/"

    def _post(self, payload):
        self.client.force_authenticate(self.admin)
        return self.client.post(self.url, payload, format="json")

    def test_preview_is_superuser_only(self):
        payload = {"section_type": "stories_query", "config": {"sort": "newest"}}
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, 401)
        self.client.force_authenticate(self.regular)
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, 403)

    def test_story_video_and_blog_preview_shapes_and_full_totals(self):
        story_response = self._post({
            "section_type": "stories_query", "item_limit": 3,
            "allow_repeats": True, "config": {"sort": "popular"},
        })
        self.assertEqual(story_response.status_code, 200, story_response.content)
        self.assertEqual(story_response.json()["total_matching"], 8)
        self.assertEqual(len(story_response.json()["items"]), 3)
        self.assertTrue({"id", "title", "slug"}.issubset(story_response.json()["items"][0]))

        video_response = self._post({
            "section_type": "videos", "item_limit": 6,
            "config": {"video_ids": [self.video.pk, 999999]},
        })
        self.assertEqual(video_response.status_code, 200, video_response.content)
        self.assertEqual(video_response.json()["total_matching"], 1)
        self.assertTrue({"id", "display_title", "youtube_id"}.issubset(video_response.json()["items"][0]))

        blog_response = self._post({
            "section_type": "blogs", "item_limit": 6,
            "config": {"blog_ids": [self.blog.pk, 999999]},
        })
        self.assertEqual(blog_response.status_code, 200, blog_response.content)
        self.assertEqual(blog_response.json()["total_matching"], 1)
        self.assertTrue({"id", "title", "published_at"}.issubset(blog_response.json()["items"][0]))

    def test_invalid_config_returns_clean_message_and_non_content_is_rejected(self):
        invalid = self._post({
            "section_type": "stories_query", "config": {"min_reading_minutes": 0}
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            invalid.json(),
            {"detail": "min_reading_minutes must be an integer from 1 to 600."},
        )
        non_content = self._post({"section_type": "banner", "config": {}})
        self.assertEqual(non_content.status_code, 400)

    def test_preview_never_writes_home_sections(self):
        before = self.HomeSection.objects.count()
        response = self._post({
            "section_type": "stories_query", "variant": "standard",
            "item_limit": 4, "allow_repeats": False, "config": {"sort": "az"},
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.HomeSection.objects.count(), before)

    def test_preview_items_equal_the_saved_homepage_section(self):
        from apps.story.home_sections import clean_config

        config = clean_config("stories_query", {"sort": "az", "countries": []})
        self.HomeSection.objects.create(
            section_type="stories_query", title="Preview comparison", position=0,
            item_limit=4, allow_repeats=True, config=config,
        )
        preview = self._post({
            "section_type": "stories_query", "variant": "standard",
            "item_limit": 4, "allow_repeats": True, "config": config,
        }).json()
        self.client.force_authenticate(None)
        cache.clear()
        homepage = self.client.get("/api/home/", **self.UA).json()["sections"][0]
        self.assertEqual(preview["items"], homepage["items"])


class FeaturedVideoTests(APITestCase):
    UA = {"HTTP_USER_AGENT": "Mozilla/5.0 Chrome"}

    def setUp(self):
        self.admin = User.objects.create(
            email="featadmin@example.com", username="featadmin", is_superuser=True, is_staff=True, is_active=True
        )
        self.story = Story.objects.create(title="Feat Story", slug="feat-story", is_published=True)
        self.video = Video.objects.create(title="Feat Video", slug="feat-video", youtube_id="abcdefghijk", youtube_url="https://youtu.be/abcdefghijk")
        self.draft_story = Story.objects.create(title="Draft", slug="draft-feat", is_published=False)
        self.hidden_video = Video.objects.create(
            story=self.draft_story, title="Hidden", slug="hidden-video", youtube_id="zzzzzzzzzzz", youtube_url="https://youtu.be/zzzzzzzzzzz"
        )

    def _home(self):
        cache.clear()
        return self.client.get("/api/home/", **self.UA).json()

    def test_lineup_mixes_stories_and_videos_in_order(self):
        self.client.force_authenticate(self.admin)
        put = self.client.put(
            "/api/admin/stories/featured/",
            {"items": [{"kind": "video", "id": self.video.id}, {"kind": "story", "id": self.story.id}]},
            format="json",
        )
        self.assertEqual(put.status_code, 200)
        self.assertEqual([(r["kind"], r["featured_rank"]) for r in put.json()], [("video", 1), ("story", 2)])
        self.client.force_authenticate(None)
        data = self._home()
        self.assertEqual([i["kind"] for i in data["featured_items"]], ["video", "story"])
        self.assertEqual(data["featured_items"][0]["video"]["id"], self.video.id)
        # Backfill must not kick in once any manual pick exists.
        self.assertEqual([s["id"] for s in data["featured_stories"]], [self.story.id])

    def test_video_only_lineup_does_not_backfill_stories(self):
        self.client.force_authenticate(self.admin)
        self.client.put("/api/admin/stories/featured/", {"items": [{"kind": "video", "id": self.video.id}]}, format="json")
        self.client.force_authenticate(None)
        data = self._home()
        self.assertEqual([i["kind"] for i in data["featured_items"]], ["video"])
        self.assertEqual(data["featured_stories"], [])

    def test_unwatchable_video_is_not_shown_and_bad_payload_changes_nothing(self):
        self.client.force_authenticate(self.admin)
        self.client.put("/api/admin/stories/featured/", {"items": [{"kind": "story", "id": self.story.id}]}, format="json")
        bad = self.client.put("/api/admin/stories/featured/", {"items": [{"kind": "video", "id": 99999}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(len(self.client.get("/api/admin/stories/featured/").json()), 1)
        self.client.put(
            "/api/admin/stories/featured/",
            {"items": [{"kind": "video", "id": self.hidden_video.id}, {"kind": "story", "id": self.story.id}]},
            format="json",
        )
        self.client.force_authenticate(None)
        self.assertEqual([i["kind"] for i in self._home()["featured_items"]], ["story"])

    def test_legacy_story_ids_payload_still_works(self):
        self.client.force_authenticate(self.admin)
        put = self.client.put("/api/admin/stories/featured/", {"story_ids": [self.story.id]}, format="json")
        self.assertEqual([r["kind"] for r in put.json()], ["story"])

    def test_daily_can_be_a_video_and_switches_targets(self):
        self.client.force_authenticate(self.admin)
        today = timezone.localdate().isoformat()
        r = self.client.put("/api/admin/stories/daily/", {"date": today, "video": self.video.id, "featured_reason": "Watch this"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["video_detail"]["id"], self.video.id)
        self.assertIsNone(r.json()["story"])
        self.client.force_authenticate(None)
        daily = self._home()["daily_story"]
        self.assertTrue(daily["configured"])
        self.assertEqual(daily["video"]["id"], self.video.id)
        self.assertIsNone(daily["story"])
        self.client.force_authenticate(self.admin)
        r = self.client.put("/api/admin/stories/daily/", {"date": today, "story": self.story.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.json()["video"])
        both = self.client.put("/api/admin/stories/daily/", {"date": today}, format="json")
        # no target in the payload keeps the existing one, so still valid
        self.assertEqual(both.status_code, 200)


class HomepageImprovementsTests(APITestCase):
    """Honest hero stats, trending sort, audience/schedule gating, require_cover,
    click tracking and the recommended-layout seed."""

    UA = {"HTTP_USER_AGENT": "Mozilla/5.0 Chrome"}

    def setUp(self):
        from apps.story.models import HomeSection

        cache.clear()
        self.HomeSection = HomeSection
        HomeSection.objects.all().delete()
        HeroTemplate.objects.all().delete()
        self.now = timezone.now()
        self.admin = User.objects.create(
            email="hpadmin@example.com", username="hpadmin", is_superuser=True, is_staff=True, is_active=True
        )
        self.reader = User.objects.create(email="hpreader@example.com", username="hpreader", is_active=True)

    def _story(self, n, **extra):
        return Story.objects.create(
            title=f"HP Story {n}", slug=f"hp-story-{n}", is_published=True, cover_image="https://example.com/c.jpg", **extra
        )

    def _event(self, story, visitor, days_ago=1, event_type=None, **extra):
        event = AnalyticsEvent.objects.create(
            event_type=event_type or AnalyticsEvent.EVENT_READING_SESSION,
            visitor_id=visitor, story=story, **extra,
        )
        AnalyticsEvent.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days_ago))
        return event

    def _home(self, **headers):
        cache.clear()
        return self.client.get("/api/home/", **{**self.UA, **headers}).json()

    def _sections(self, **headers):
        return self._home(**headers)["sections"]

    # -- hero stats ---------------------------------------------------------

    def test_hero_stats_are_honest_and_cached(self):
        a = self._story(1, country="NP", language="en", views=500)
        b = self._story(2, country="IN", language="ne", views=500)
        self._story(3, country="NP", language="en")
        self._event(a, "v1")
        self._event(a, "v1", days_ago=2)  # same person twice
        self._event(b, "v2")
        self._event(b, "v-old", days_ago=40)  # outside 30 days
        self._event(b, "v-bot", is_suspected_bot=True)
        self._event(b, "v-staff", user=self.admin)
        stats = self._home()["sidebar"]["stats"]
        self.assertEqual(stats["readers_30d"], 2)
        self.assertEqual(stats["countries"], 2)
        self.assertEqual(stats["languages"], 2)
        self.assertEqual(stats["stories"], 3)
        self.assertEqual(stats["readers"], 1000)  # legacy key still lifetime views

        self._event(b, "v3")
        from apps.story.home_metrics import HOME_STATS_CACHE_KEY, get_home_stats

        self.assertIsNotNone(cache.get(HOME_STATS_CACHE_KEY))
        self.assertEqual(get_home_stats()["readers_30d"], 2)  # served from cache
        cache.clear()
        self.assertEqual(get_home_stats()["readers_30d"], 3)

    def test_hero_resolves_new_stat_sources(self):
        a = self._story(1, country="NP", language="en")
        self._event(a, "v1")
        HeroTemplate.objects.create(
            name="Honest", is_default=True,
            stat_1_source="readers_30d", stat_1_label="readers this month",
            stat_2_source="countries", stat_2_label="countries",
            stat_3_source="languages", stat_3_label="languages",
        )
        hero = self._home()["hero"]
        self.assertEqual([s["value"] for s in hero["stats"]], [1, 1, 1])

    # -- trending -----------------------------------------------------------

    def _trending_section(self, **config):
        return self.HomeSection.objects.create(
            section_type="stories_query", title="Trending", position=0, item_limit=10,
            allow_repeats=True, config={"sort": "trending", **config},
        )

    def test_trending_ranks_by_recent_unique_audience_readers_only(self):
        stories = [self._story(n) for n in range(6)]
        # story 0 has many OLD events; stories 1..5 have recent readers, 5 the most.
        for i in range(10):
            self._event(stories[0], f"old{i}", days_ago=20)
        for n in range(1, 6):
            for i in range(n):
                self._event(stories[n], f"r{n}-{i}", days_ago=2)
        self._event(stories[1], "bot", is_suspected_bot=True)
        self._event(stories[1], "staff", user=self.admin)
        self._trending_section()
        items = self._sections()[0]["items"]
        self.assertEqual([i["title"] for i in items], [f"HP Story {n}" for n in (5, 4, 3, 2, 1)])

    def test_trending_falls_back_to_30_days_when_few_stories_qualify(self):
        a, b = self._story(1), self._story(2)
        self._event(a, "x", days_ago=15)
        self._event(b, "y1", days_ago=20)
        self._event(b, "y2", days_ago=20)
        self._trending_section()
        titles = [i["title"] for i in self._sections()[0]["items"]]
        self.assertEqual(titles, ["HP Story 2", "HP Story 1"])

    def test_trending_ids_are_cached(self):
        from apps.story.home_metrics import TRENDING_CACHE_KEY, trending_story_ids

        a = self._story(1)
        self._event(a, "x")
        self.assertEqual(trending_story_ids(), [a.pk])
        self.assertEqual(cache.get(TRENDING_CACHE_KEY), [a.pk])
        self._event(self._story(2), "y")
        self.assertEqual(trending_story_ids(), [a.pk])

    def test_trending_is_an_admin_sort_option(self):
        self.client.force_authenticate(self.admin)
        sorts = self.client.get("/api/admin/home-sections/types/").json()["sorts"]["stories"]
        self.assertIn("trending", [option["value"] for option in sorts])
        r = self.client.post(
            "/api/admin/home-sections/",
            {"section_type": "stories_query", "title": "T", "layout": "trending", "config": {"sort": "trending"}},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)

    # -- audience and schedule ----------------------------------------------

    def test_show_to_filters_by_sign_in_state(self):
        self._story(1)
        for position, show_to in enumerate(["all", "signed_in", "signed_out"]):
            self.HomeSection.objects.create(
                section_type="stories_query", title=show_to, position=position, show_to=show_to
            )
        self.assertEqual([s["title"] for s in self._sections()], ["all", "signed_out"])
        self.client.force_authenticate(self.reader)
        self.assertEqual([s["title"] for s in self._sections()], ["all", "signed_in"])

    def test_schedule_window_is_respected(self):
        self._story(1)

        def mk(title, pos, **kw):
            return self.HomeSection.objects.create(section_type="stories_query", title=title, position=pos, **kw)

        mk("live", 0, starts_at=self.now - timedelta(days=1), ends_at=self.now + timedelta(days=1))
        mk("future", 1, starts_at=self.now + timedelta(hours=1))
        mk("expired", 2, ends_at=self.now - timedelta(minutes=1))
        mk("open", 3)
        self.assertEqual([s["title"] for s in self._sections()], ["live", "open"])

    def test_admin_rejects_schedule_ending_before_it_starts(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            "/api/admin/home-sections/",
            {
                "section_type": "explore_tiles", "title": "x",
                "starts_at": (self.now + timedelta(days=2)).isoformat(),
                "ends_at": (self.now + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("ends_at", r.json())

    def test_explore_tiles_is_a_served_widget_section(self):
        self.HomeSection.objects.create(section_type="explore_tiles", title="Explore", position=0)
        self.assertEqual([s["section_type"] for s in self._sections()], ["explore_tiles"])

    # -- require_cover ------------------------------------------------------

    def test_require_cover_excludes_stories_without_any_cover(self):
        with_url = self._story(1)
        no_cover = Story.objects.create(title="No cover", slug="no-cover", is_published=True)
        section = self.HomeSection.objects.create(
            section_type="stories_query", title="Covers", position=0, config={"sort": "newest"}
        )
        self.assertEqual(len(self._sections()[0]["items"]), 2)
        section.config = {"sort": "newest", "require_cover": True}
        section.save()
        titles = [i["title"] for i in self._sections()[0]["items"]]
        self.assertEqual(titles, [with_url.title])

        self.client.force_authenticate(self.admin)
        rows = self.client.get("/api/admin/home-sections/missing-covers/").json()
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["results"][0]["id"], no_cover.pk)

    # -- click tracking -----------------------------------------------------

    def test_ingest_accepts_home_section_click(self):
        from uuid import uuid4

        r = self.client.post(
            reverse("analytics-events"),
            {
                "event_id": str(uuid4()), "event_type": "home_section_click", "visitor_id": "vis",
                "metadata": {"section_id": 7, "section_type": "stories_query", "position": 2, "item_id": 5, "item_type": "story"},
            },
            format="json", **self.UA,
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(AnalyticsEvent.objects.get(event_type="home_section_click").metadata["section_id"], 7)

    def test_admin_click_stats_are_audience_filtered_and_windowed(self):
        section = self.HomeSection.objects.create(section_type="stories_query", title="S", position=0)

        def click(visitor, days_ago, **kw):
            e = AnalyticsEvent.objects.create(
                event_type="home_section_click", visitor_id=visitor, metadata={"section_id": section.pk}, **kw
            )
            AnalyticsEvent.objects.filter(pk=e.pk).update(created_at=self.now - timedelta(days=days_ago))

        def visit(visitor, days_ago, path="/"):
            e = AnalyticsEvent.objects.create(event_type="visit", visitor_id=visitor, metadata={"path": path})
            AnalyticsEvent.objects.filter(pk=e.pk).update(created_at=self.now - timedelta(days=days_ago))

        for i in range(4):
            visit(f"h{i}", 1)
        visit("h-old", 20)
        visit("elsewhere", 1, path="/library")
        click("a", 1)
        click("b", 3)
        click("c", 20)
        click("bot", 1, is_suspected_bot=True)
        click("staff", 1, user=self.admin)

        self.client.force_authenticate(self.admin)
        data = self.client.get("/api/admin/home-sections/analytics/").json()
        self.assertEqual(data["visits"], {"7": 4, "30": 5})
        entry = data["sections"][str(section.pk)]
        self.assertEqual(entry["7"]["clicks"], 2)
        self.assertEqual(entry["7"]["click_through"], 0.5)
        self.assertEqual(entry["30"]["clicks"], 3)

    # -- seeding ------------------------------------------------------------

    def test_seed_creates_layout_only_when_no_sections_exist(self):
        from django.core.management import call_command

        call_command("seed_home_sections", stdout=io.StringIO())
        count = self.HomeSection.objects.count()
        self.assertGreater(count, 5)
        types = list(self.HomeSection.objects.values_list("section_type", flat=True))
        self.assertIn("explore_tiles", types)
        self.assertNotIn("journeys_banner", types)

        mine = self.HomeSection.objects.first()
        mine.title = "Custom"
        mine.save()
        call_command("seed_home_sections", stdout=io.StringIO())
        self.assertEqual(self.HomeSection.objects.count(), count)
        self.assertEqual(self.HomeSection.objects.get(pk=mine.pk).title, "Custom")


class LibraryFilterTests(APITestCase):
    """The Library's list filters, sorts, facets, collections and bulk progress."""

    def setUp(self):
        from apps.story.models import Author, Category, Genre, StoryType

        cache.clear()
        self.client.defaults["HTTP_USER_AGENT"] = "Mozilla/5.0 Chrome"
        self.fantasy = Genre.objects.get_or_create(slug="fantasy", defaults={"name": "Fantasy"})[0]
        self.mystery = Genre.objects.get_or_create(slug="mystery", defaults={"name": "Mystery"})[0]
        self.folk = Category.objects.get_or_create(slug="folk-tales", defaults={"name": "Folk Tales"})[0]
        self.novel = StoryType.objects.get_or_create(name="Novel")[0]
        self.short = StoryType.objects.get_or_create(name="Short Story")[0]
        self.doyle = Author.objects.create(name="Arthur Conan Doyle")
        self.rai = Author.objects.create(name="Asha Rai")
        self.reader = User.objects.create_user(email="lib@example.com", username="libreader", password="x", is_active=True)

        def make(slug, **kw):
            genres = kw.pop("genres", [])
            categories = kw.pop("categories", [])
            story = Story.objects.create(
                title=kw.pop("title", slug.replace("-", " ").title()), slug=slug, is_published=True, **kw
            )
            story.genres.set(genres)
            story.categories.set(categories)
            return story

        self.a = make("alpha", genres=[self.fantasy], categories=[self.folk], language="en", country="NP", author=self.rai,
                      story_type=self.short, cached_chapter_reading_minutes=5, is_original=True, views=50, rating=4.0)
        self.b = make("bravo", genres=[self.fantasy, self.mystery], language="ne", country="NP", author=self.rai,
                      story_type=self.novel, cached_chapter_reading_minutes=45, views=10, rating=5.0, is_completed=True)
        self.c = make("charlie", genres=[self.mystery], language="en", country="GB", author=self.doyle,
                      story_type=self.novel, cached_chapter_reading_minutes=120, views=30, rating=3.0, is_completed=True)
        self.d = make("delta", genres=[], language="en", country="", author=self.doyle,
                      story_type=self.novel, cached_chapter_reading_minutes=400, views=5, rating=1.0)
        self.e = make("echo", language="en", views=1)  # unknown length
        Audio.objects.create(story=self.a, title="A", slug="a-audio", audio_file="story_audios/a.mp3", order=1,
                             transcript="<p>words</p>")
        Audio.objects.create(story=self.b, title="B", slug="b-audio", audio_file="story_audios/b.mp3", order=1)
        Video.objects.create(story=self.c, title="C", slug="c-video", youtube_url="https://youtu.be/dQw4w9WgXcQ", youtube_id="dQw4w9WgXcQ")
        Story.objects.filter(pk=self.d.pk).update(summary="<p>Short summary</p>")
        Chapter.objects.create(story=self.a, title="Ch", slug="a-ch1", content="<p>x</p>", order=1)
        # The chapter signal recomputes the cache; pin the value the tests rely on.
        Story.objects.filter(pk=self.a.pk).update(cached_chapter_reading_minutes=5)

    def _slugs(self, **params):
        response = self.client.get(reverse("story-list"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["slug"] for row in response.json()["results"]]

    def _set(self, **params):
        return set(self._slugs(**params))

    # --- filters ---------------------------------------------------------------

    def test_genre_and_category_filter_by_slug_or_id(self):
        self.assertEqual(self._set(genre="fantasy"), {"alpha", "bravo"})
        self.assertEqual(self._set(genre=str(self.fantasy.pk)), {"alpha", "bravo"})
        self.assertEqual(self._set(genre="fantasy,mystery"), {"alpha", "bravo", "charlie"})
        self.assertEqual(self._set(category="folk-tales"), {"alpha"})
        self.assertEqual(self._set(genres=str(self.mystery.pk)), {"bravo", "charlie"})  # legacy id param still works

    def test_length_buckets_use_the_reading_estimate(self):
        self.assertEqual(self._set(length="under_10"), {"alpha"})
        self.assertEqual(self._set(length="10_60"), {"bravo"})
        self.assertEqual(self._set(length="1_5h"), {"charlie"})
        self.assertEqual(self._set(length="5h_plus"), {"delta"})  # unknown length (echo) is in no bucket
        self.assertEqual(self._set(length="bogus"), {"alpha", "bravo", "charlie", "delta", "echo"})

    def test_country_author_original_and_status(self):
        self.assertEqual(self._set(country="NP"), {"alpha", "bravo"})
        self.assertEqual(self._set(author=str(self.doyle.pk)), {"charlie", "delta"})
        self.assertEqual(self._set(author="arthur-conan-doyle"), {"charlie", "delta"})
        self.assertEqual(self._set(author="nobody"), set())
        self.assertEqual(self._set(original="true"), {"alpha"})
        self.assertEqual(self._set(is_original="true"), {"alpha"})
        self.assertEqual(self._set(status="completed"), {"bravo", "charlie"})

    def test_format_filter_maps_to_the_availability_flags(self):
        self.assertEqual(self._set(format="listen"), {"alpha", "bravo"})
        self.assertEqual(self._set(format="watch"), {"charlie"})
        self.assertEqual(self._set(format="read_along"), {"alpha"})
        self.assertEqual(self._set(format="read-along"), {"alpha"})
        self.assertEqual(self._set(format="quick_read"), {"delta"})
        self.assertEqual(self._set(format="read"), {"alpha"})  # only story with chapters/files

    def test_filters_combine(self):
        self.assertEqual(self._set(genre="fantasy", language="ne"), {"bravo"})
        self.assertEqual(self._set(genre="fantasy", length="under_10", format="listen"), {"alpha"})
        self.assertEqual(self._set(country="NP", original="true", status="ongoing"), {"alpha"})
        self.assertEqual(self._set(genre="mystery", author=str(self.rai.pk)), {"bravo"})
        self.assertEqual(self._set(genre="fantasy", length="5h_plus"), set())

    def test_new_sorts(self):
        self.assertEqual(self._slugs(sort="title")[:3], ["alpha", "bravo", "charlie"])
        self.assertEqual(self._slugs(sort="shortest"), ["alpha", "bravo", "charlie", "delta", "echo"])  # unknown last
        self.assertEqual(self._slugs(sort="popular")[:3], ["alpha", "charlie", "bravo"])  # most viewed first
        self.assertEqual(self._slugs(sort="rating")[:2], ["bravo", "alpha"])

    def test_list_serializer_carries_what_the_card_needs_without_n_plus_one(self):
        response = self.client.get(reverse("story-list"), {"sort": "title"})
        row = response.json()["results"][0]
        for key in ("author", "reading_time_minutes", "language", "has_audio", "has_video", "has_read_along",
                    "is_original", "site_published_date", "country", "has_text"):
            self.assertIn(key, row)
        self.assertEqual((row["author"], row["reading_time_minutes"], row["country"]), ("Asha Rai", 5, "NP"))
        by_slug = {r["slug"]: r["has_text"] for r in response.json()["results"]}
        self.assertTrue(by_slug["alpha"])  # has a chapter
        self.assertFalse(by_slug["bravo"])
        counts = []
        for _ in range(2):
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("story-list"), {"sort": "title"})
            counts.append(len(ctx))
        for n in range(6):
            extra = Story.objects.create(title=f"Extra {n}", slug=f"extra-{n}", is_published=True, author=self.rai)
            extra.genres.set([self.fantasy])
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("story-list"), {"sort": "title"})
        self.assertEqual(len(ctx), counts[0])

    # --- facets ------------------------------------------------------------------

    def _facets(self, **params):
        cache.clear()
        response = self.client.get(reverse("story-facets"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    @staticmethod
    def _by(rows, key):
        return {row[key]: row["count"] for row in rows}

    def test_facets_count_the_whole_catalogue_with_no_filters(self):
        data = self._facets()
        self.assertEqual(data["total"], 5)
        self.assertEqual(self._by(data["genres"], "slug"), {"fantasy": 2, "mystery": 2})
        self.assertEqual(self._by(data["languages"], "value"), {"en": 4, "ne": 1})
        self.assertEqual(self._by(data["countries"], "value"), {"NP": 2, "GB": 1})
        self.assertEqual(self._by(data["lengths"], "value"), {"under_10": 1, "10_60": 1, "1_5h": 1, "5h_plus": 1})
        self.assertEqual(self._by(data["statuses"], "value"), {"completed": 2, "ongoing": 3})
        self.assertEqual(self._by(data["formats"], "value"),
                         {"all": 5, "read": 1, "listen": 2, "read_along": 1, "watch": 1, "quick_read": 1})
        self.assertEqual(data["originals"]["count"], 1)
        self.assertEqual(self._by(data["types"], "name"), {"Novel": 3, "Short Story": 2})  # echo has the default type

    def test_each_facet_is_counted_under_the_other_active_filters(self):
        data = self._facets(language="en")
        # genres under language=en: bravo (ne) drops out
        self.assertEqual(self._by(data["genres"], "slug"), {"fantasy": 1, "mystery": 1})
        self.assertEqual(data["total"], 4)
        # the language facet ignores its own filter, so "ne" is still offered
        self.assertEqual(self._by(data["languages"], "value"), {"en": 4, "ne": 1})
        self.assertEqual(self._by(data["countries"], "value"), {"NP": 1, "GB": 1})

    def test_multi_select_facet_keeps_sibling_options(self):
        data = self._facets(genre="fantasy", length="10_60")
        # genre facet ignores `genre`, applies `length`: only bravo is 10-60 min
        self.assertEqual(self._by(data["genres"], "slug"), {"fantasy": 1, "mystery": 1})
        # length facet ignores `length`, applies `genre=fantasy`
        self.assertEqual(self._by(data["lengths"], "value"), {"under_10": 1, "10_60": 1, "1_5h": 0, "5h_plus": 0})
        self.assertEqual(data["total"], 1)

    def test_format_facet_counts_other_filters_but_not_format(self):
        data = self._facets(format="listen", genre="mystery")
        self.assertEqual(self._by(data["formats"], "value")["all"], 2)  # bravo + charlie
        self.assertEqual(self._by(data["formats"], "value")["watch"], 1)
        self.assertEqual(data["total"], 1)  # only bravo is mystery AND listenable

    def test_facets_are_cached(self):
        self._facets()
        Story.objects.create(title="Late", slug="late", is_published=True, language="fr")
        response = self.client.get(reverse("story-facets"))
        self.assertEqual(response.json()["total"], 5)  # served from cache
        cache.clear()
        self.assertEqual(self.client.get(reverse("story-facets")).json()["total"], 6)

    # --- collections ---------------------------------------------------------------

    def test_collections_shape_and_params_reproduce_the_list(self):
        cache.clear()
        for story in Story.objects.all():
            Story.objects.filter(pk=story.pk).update(cover_image=f"https://example.com/{story.slug}.jpg")
        for n in range(3):  # enough Nepali stories to earn a tile
            Story.objects.create(title=f"Nepali {n}", slug=f"nep-{n}", is_published=True, language="ne",
                                 cover_image=f"https://example.com/n{n}.jpg")
        response = self.client.get(reverse("story-collections"))
        self.assertEqual(response.status_code, 200)
        tiles = response.json()["results"]
        keys = [tile["key"] for tile in tiles]
        self.assertIn("nepali", keys)
        self.assertNotIn("originals", keys)  # one original is below the minimum
        for tile in tiles:
            self.assertEqual(set(tile), {"key", "label", "params", "count", "covers"})
            self.assertLessEqual(len(tile["covers"]), 3)
            self.assertGreaterEqual(tile["count"], 3)
            listed = self.client.get(reverse("story-list"), tile["params"]).json()["pagination"]["count"]
            self.assertEqual(listed, tile["count"], tile["key"])
        nepali = next(tile for tile in tiles if tile["key"] == "nepali")
        self.assertEqual(nepali["params"], {"language": "ne"})
        self.assertEqual(len(nepali["covers"]), 3)

    def test_collections_are_cached_for_an_hour(self):
        from apps.story import library_api

        cache.clear()
        for n in range(3):
            Story.objects.create(title=f"N{n}", slug=f"n{n}", is_published=True, language="ne")
        self.client.get(reverse("story-collections"))
        Story.objects.create(title="N9", slug="n9", is_published=True, language="ne")
        again = self.client.get(reverse("story-collections")).json()["results"]
        self.assertEqual(next(t for t in again if t["key"] == "nepali")["count"], 4)  # bravo + the three, not the fifth
        self.assertEqual(library_api.COLLECTIONS_CACHE_SECONDS, 3600)

    # --- bulk progress ---------------------------------------------------------------

    def test_progress_requires_sign_in_and_returns_many_stories_at_once(self):
        from apps.stats.models import AudioReadingProgress, ChapterReadingProgress, StoryCompletion

        ids = f"{self.a.pk},{self.b.pk},{self.c.pk},{self.d.pk}"
        self.assertEqual(self.client.get(reverse("story-progress"), {"ids": ids}).status_code, 401)

        chapter = Chapter.objects.get(slug="a-ch1")
        ChapterReadingProgress.objects.create(user=self.reader, story=self.a, chapter=chapter, progress=0.4)
        AudioReadingProgress.objects.create(user=self.reader, story=self.b, audio=Audio.objects.get(slug="b-audio"), progress=0.75)
        StoryCompletion.objects.create(user=self.reader, story=self.c, source="chapters")
        self.client.force_authenticate(self.reader)
        data = self.client.get(reverse("story-progress"), {"ids": ids}).json()["progress"]
        self.assertEqual(data, {str(self.a.pk): 0.4, str(self.b.pk): 0.75, str(self.c.pk): 1.0})
        self.assertEqual(self.client.get(reverse("story-progress")).json(), {"progress": {}})


class DiscoverDataTests(APITestCase):
    """Discover: trending on recent audience events, hidden gems, just added,
    countries, popular genres and the surprise pick."""

    def setUp(self):
        cache.clear()
        self.client.defaults["HTTP_USER_AGENT"] = "Mozilla/5.0 Chrome"
        self.now = timezone.now()
        self.staff = User.objects.create_user(email="dstaff@example.com", username="dstaff", password="x", is_staff=True, is_active=True)
        self.member = User.objects.create_user(email="dmember@example.com", username="dmember", password="x", is_active=True)
        self.n = 0

    def _story(self, slug, **kw):
        kw.setdefault("cover_image", "https://example.com/c.jpg")
        return Story.objects.create(title=slug.replace("-", " ").title(), slug=slug, is_published=True, **kw)

    def _event(self, story, visitor, event_type="reading_session", days_ago=1, **kw):
        event = AnalyticsEvent.objects.create(event_type=event_type, visitor_id=visitor, story=story, **kw)
        AnalyticsEvent.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days_ago, hours=1))
        return event

    def _readers(self, story, count, *, prefix=None, days_ago=1, event_type="reading_session"):
        for i in range(count):
            self._event(story, f"{prefix or story.slug}-{i}", event_type, days_ago)

    def _get(self):
        cache.clear()
        response = self.client.get(reverse("discover-data"))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    # --- trending ---------------------------------------------------------------

    def test_trending_uses_recent_unique_audience_readers_not_lifetime_views(self):
        famous = self._story("famous", views=999_999)  # lifetime views must not matter
        rising = self._story("rising", views=1)
        quiet = self._story("quiet")
        self._readers(famous, 8, days_ago=20)  # old activity, outside 7 days
        self._readers(rising, 6)
        self._readers(quiet, 5)
        # the same person reading twice is one reader
        self._event(rising, "rising-0")
        self._event(rising, "rising-0", "listening_session")
        # staff, a suspected bot and a draft never count
        self._event(quiet, "staff", user=self.staff)
        for i in range(20):
            self._event(quiet, f"bot-{i}", is_suspected_bot=True)

        rows = self._get()["trending"]
        self.assertEqual([r["story"]["slug"] for r in rows], ["rising", "quiet"])
        self.assertEqual([r["readers"] for r in rows], [6, 5])
        self.assertEqual({r["window_days"] for r in rows}, {7})
        self.assertNotIn("famous", [r["story"]["slug"] for r in rows])

    def test_trending_falls_back_to_30_days_when_the_week_is_too_quiet(self):
        a, b = self._story("older-hit"), self._story("older-second")
        self._readers(a, 7, days_ago=15)
        self._readers(b, 3, days_ago=20)
        self._readers(self._story("tiny"), 2, days_ago=2)  # top of the 7-day window, but only 2 readers
        rows = self._get()["trending"]
        self.assertEqual({r["window_days"] for r in rows}, {30})
        self.assertEqual(rows[0]["story"]["slug"], "older-hit")

    def test_trending_rank_change_compares_with_the_window_before(self):
        climbing, falling, fresh = self._story("climbing"), self._story("falling"), self._story("fresh")
        # previous week (8-14 days ago): falling 9 readers, climbing 5
        self._readers(falling, 9, days_ago=10)
        self._readers(climbing, 5, days_ago=10)
        # this week: climbing 8, falling 6, fresh 5 (not ranked before)
        self._readers(climbing, 8, prefix="c-now")
        self._readers(falling, 6, prefix="f-now")
        self._readers(fresh, 5, prefix="n-now")
        rows = {r["story"]["slug"]: r for r in self._get()["trending"]}
        self.assertEqual(rows["climbing"]["rank_change"], 1)   # second -> first
        self.assertEqual(rows["falling"]["rank_change"], -1)   # first -> second
        self.assertIsNone(rows["fresh"]["rank_change"])

    def test_trending_ranking_is_cached(self):
        a = self._story("cached-a")
        self._readers(a, 5)
        self._get()
        self._readers(self._story("cached-b"), 9)
        response = self.client.get(reverse("discover-data")).json()  # cache_page + ranking cache still warm
        self.assertEqual([r["story"]["slug"] for r in response["trending"]], ["cached-a"])

    # --- hidden gems --------------------------------------------------------------

    def _gem_activity(self, story, starters, finishers, *, extra_viewers=0, days_ago=10):
        for i in range(starters):
            self._event(story, f"{story.slug}-s{i}", "story_started", days_ago)
        for i in range(finishers):
            user = User.objects.create_user(email=f"{story.slug}-{i}@example.com", username=f"{story.slug}{i}", password="x", is_active=True)
            self._event(story, "server", "story_completed", days_ago, user=user)
        for i in range(extra_viewers):
            self._event(story, f"{story.slug}-v{i}", "visit", days_ago)

    def test_hidden_gem_needs_five_starters_and_below_median_viewers(self):
        gem = self._story("quiet-gem")
        popular = self._story("popular-one")
        popular_two = self._story("popular-two")
        few = self._story("too-few-starters")
        self._gem_activity(gem, starters=6, finishers=5)               # 6 viewers
        self._gem_activity(popular, starters=30, finishers=2, extra_viewers=10)  # 40 viewers
        self._gem_activity(popular_two, starters=25, finishers=20)       # 25 viewers
        self._gem_activity(few, starters=4, finishers=4)                 # under the starter bar
        rows = self._get()["hidden_gems"]
        self.assertEqual([r["story"]["slug"] for r in rows], ["quiet-gem"])
        self.assertEqual(rows[0]["reason"], "83% of readers finish it")

    def test_hidden_gems_rank_by_completion_rate(self):
        low, high = self._story("gem-low"), self._story("gem-high")
        big = self._story("crowd")
        self._gem_activity(low, starters=10, finishers=3)
        self._gem_activity(high, starters=5, finishers=5)
        self._gem_activity(big, starters=100, finishers=50, extra_viewers=50)
        self._gem_activity(self._story("crowd-two"), starters=60, finishers=1, extra_viewers=40)
        rows = self._get()["hidden_gems"]
        self.assertEqual([r["story"]["slug"] for r in rows], ["gem-high", "gem-low"])
        self.assertEqual([r["reason"] for r in rows], ["100% of readers finish it", "30% of readers finish it"])

    def test_well_reviewed_story_qualifies_with_a_review_reason(self):
        loved = self._story("well-loved")
        meh = self._story("so-so")
        for i, rating in enumerate([5, 5, 4]):
            user = User.objects.create_user(email=f"rev{i}@example.com", username=f"rev{i}", password="x", is_active=True)
            Review.objects.create(story=loved, user=user, rating=rating)
        for i, rating in enumerate([5, 4, 4]):
            user = User.objects.create_user(email=f"mrev{i}@example.com", username=f"mrev{i}", password="x", is_active=True)
            Review.objects.create(story=meh, user=user, rating=rating)  # 4.33 average
        Review.objects.create(story=self._story("one-review"), user=self.member, rating=5)  # below 3 reviews
        rows = self._get()["hidden_gems"]
        self.assertEqual([(r["story"]["slug"], r["reason"]) for r in rows], [("well-loved", "4.7★ from 3 readers")])

    def test_hidden_gems_is_empty_when_nothing_qualifies(self):
        self._readers(self._story("lonely"), 3)
        self.assertEqual(self._get()["hidden_gems"], [])

    # --- just added ----------------------------------------------------------------

    def test_just_added_lists_each_story_once_under_its_newest_format(self):
        from datetime import date

        today = date.today()
        text_only = self._story("text-only", site_published_date=today - timedelta(days=5))
        Chapter.objects.create(story=text_only, title="C", slug="to-c1", content="<p>x</p>", order=1)
        audio_only = self._story("audio-only", site_published_date=today - timedelta(days=3))
        Audio.objects.create(story=audio_only, title="A", slug="ao-a", audio_file="story_audios/a.mp3", order=1)
        both = self._story("text-and-video", site_published_date=today - timedelta(days=30))
        Chapter.objects.create(story=both, title="C", slug="tv-c1", content="<p>x</p>", order=1)
        Video.objects.create(story=both, title="V", slug="tv-v", youtube_url="https://youtu.be/dQw4w9WgXcQ", youtube_id="dQw4w9WgXcQ")  # added today
        summary = self._story("summary-only", site_published_date=today - timedelta(days=8), summary="<p>Short.</p>")
        draft = Story.objects.create(title="Draft", slug="draft-added", is_published=False, site_published_date=today)

        rows = self._get()["just_added"]
        slugs = [r["story"]["slug"] for r in rows]
        self.assertEqual(len(slugs), len(set(slugs)))  # nobody twice
        self.assertNotIn("draft-added", slugs)
        self.assertEqual(
            {r["story"]["slug"]: r["format"] for r in rows},
            {"text-and-video": "watch", "audio-only": "listen", "text-only": "read", "summary-only": "quick_read"},
        )
        self.assertEqual(slugs, ["text-and-video", "audio-only", "text-only", "summary-only"])  # newest first
        self.assertIsNotNone(draft.pk)

    def test_just_added_prefers_the_richer_format_on_a_tie(self):
        from datetime import date

        story = self._story("tie", site_published_date=date.today(), summary="<p>S</p>")
        Chapter.objects.create(story=story, title="C", slug="tie-c1", content="<p>x</p>", order=1)
        Audio.objects.create(story=story, title="A", slug="tie-a", audio_file="story_audios/t.mp3", order=1)
        rows = self._get()["just_added"]
        self.assertEqual([(r["story"]["slug"], r["format"]) for r in rows], [("tie", "listen")])

    def test_just_added_is_capped_at_24(self):
        from datetime import date

        for i in range(30):
            s = self._story(f"bulk-{i}", site_published_date=date.today() - timedelta(days=i))
            Chapter.objects.create(story=s, title="C", slug=f"bulk-c-{i}", content="<p>x</p>", order=1)
        self.assertEqual(len(self._get()["just_added"]), 24)

    # --- countries / genres -----------------------------------------------------------

    def test_countries_are_the_top_eight_by_published_story_count(self):
        counts = {"NP": 5, "IN": 4, "US": 3, "GB": 2, "DE": 2, "JP": 1, "FR": 1, "BR": 1, "RU": 1, "KR": 1}
        for code, n in counts.items():
            for i in range(n):
                self._story(f"{code.lower()}-{i}", country=code)
        Story.objects.create(title="Hidden NP", slug="hidden-np", is_published=False, country="NP")
        self._story("no-country")
        rows = self._get()["countries"]
        self.assertEqual(len(rows), 8)
        self.assertEqual([(r["code"], r["name"], r["count"]) for r in rows[:3]], [("NP", "Nepal", 5), ("IN", "India", 4), ("US", "United States", 3)])
        self.assertTrue(all(len(r["covers"]) <= 3 for r in rows))
        self.assertEqual(len(rows[0]["covers"]), 3)

    def test_popular_genres_are_the_twelve_biggest_with_published_counts(self):
        from apps.story.models import Genre

        for g in range(14):
            genre = Genre.objects.create(name=f"Genre {g:02d}", slug=f"genre-{g:02d}")
            for i in range(14 - g):
                self._story(f"g{g}-{i}").genres.add(genre)
        solo = Genre.objects.create(name="Draft Only", slug="draft-only")
        Story.objects.create(title="D", slug="d-only", is_published=False).genres.add(solo)
        rows = self._get()["popular_genres"]
        self.assertEqual(len(rows), 12)
        self.assertEqual((rows[0]["name"], rows[0]["count"], rows[0]["slug"]), ("Genre 00", 14, "genre-00"))
        self.assertNotIn("Draft Only", [r["name"] for r in rows])

    def test_old_leaderboard_keys_are_gone(self):
        data = self._get()
        for key in ("most_viewed", "highest_rated", "most_favorited", "most_discussed", "new_releases", "genres", "categories", "languages"):
            self.assertNotIn(key, data)

    # --- surprise -------------------------------------------------------------------

    def test_surprise_picks_only_stories_with_a_cover_and_a_description(self):
        ok = self._story("surprise-ok", about="<p>A <b>haunted</b> lantern.</p>")
        self._story("no-cover", cover_image="", about="Has text")
        self._story("no-blurb")
        Story.objects.create(title="Draft", slug="draft-s", is_published=False, cover_image="https://x/y.jpg", about="d")
        for _ in range(5):
            data = self.client.get(reverse("discover-surprise")).json()
            self.assertEqual(data["story"]["slug"], "surprise-ok")
        self.assertEqual(data["blurb"], "A haunted lantern.")
        self.assertEqual(data["story"]["id"], ok.pk)

    def test_surprise_honours_exclude_and_recycles_when_everything_was_shown(self):
        a = self._story("sa", about="one")
        b = self._story("sb", about="two")
        for _ in range(6):
            data = self.client.get(reverse("discover-surprise"), {"exclude": str(a.pk)}).json()
            self.assertEqual(data["story"]["id"], b.pk)
            self.assertFalse(data["recycled"])
        both = self.client.get(reverse("discover-surprise"), {"exclude": f"{a.pk},{b.pk}"}).json()
        self.assertTrue(both["recycled"])
        self.assertIn(both["story"]["id"], {a.pk, b.pk})

    def test_surprise_with_no_qualifying_story_returns_null(self):
        self._story("bare")
        self.assertEqual(self.client.get(reverse("discover-surprise")).json()["story"], None)

    # --- journeys and the length filter ---------------------------------------------------

    def test_journeys_list_carries_two_covers_for_the_collage(self):
        from apps.story.models import StoryJourney, StoryJourneyItem

        journey = StoryJourney.objects.create(title="J", slug="j", active=True)
        for i in range(3):
            StoryJourneyItem.objects.create(journey=journey, story=self._story(f"j-{i}"), position=i)
        row = self.client.get(reverse("journey-list")).json()["journeys"][0]
        self.assertEqual(len(row["covers"]), 2)
        self.assertEqual((row["completed"], row["total"]), (0, 3))

    def test_story_list_filters_by_max_minutes(self):
        short = self._story("max-short", cached_chapter_reading_minutes=15)
        self._story("max-long", cached_chapter_reading_minutes=90)
        self._story("max-unknown")
        slugs = [s["slug"] for s in self.client.get(reverse("story-list"), {"max_minutes": "20"}).json()["results"]]
        self.assertEqual(slugs, ["max-short"])
        self.assertIsNotNone(short.pk)


class BlogListAndOverviewTests(APITestCase):
    """Blog topics, search, pagination, popular sort, reading time, overview and backfill commands."""

    def setUp(self):
        from apps.story.models import BlogTopic

        cache.clear()
        self.client.defaults["HTTP_USER_AGENT"] = "Mozilla/5.0 Chrome"
        self.now = timezone.now()
        self.staff = User.objects.create_user(email="bstaff@example.com", username="bstaff", password="x", is_staff=True, is_active=True)
        self.admin = User.objects.create_user(
            email="badmin@example.com", username="badmin", password="x", is_superuser=True, is_staff=True, is_active=True
        )
        self.guides = BlogTopic.objects.get(slug="reading-guides")
        self.folk = BlogTopic.objects.get(slug="folklore-myth")
        self.n = 0

    def _blog(self, title, *, topics=(), days_old=0, **kw):
        self.n += 1
        blog = Blog.objects.create(title=title, slug=f"post-{self.n}", content=kw.pop("content", "<p>Some words here.</p>"), **kw)
        blog.topics.set(topics)
        Blog.objects.filter(pk=blog.pk).update(created_at=self.now - timedelta(days=days_old))
        return blog

    def _read(self, blog, visitor, event_type="visit", days_ago=1, **kw):
        event = AnalyticsEvent.objects.create(event_type=event_type, visitor_id=visitor, blog=blog, **kw)
        AnalyticsEvent.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days_ago))

    def _list(self, **params):
        cache.clear()
        response = self.client.get(reverse("blog-list"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _titles(self, **params):
        return [row["title"] for row in self._list(**params)["results"]]

    # --- topics, search, pagination ----------------------------------------------

    def test_topics_are_seeded(self):
        from apps.story.models import BlogTopic

        self.assertEqual(
            list(BlogTopic.objects.values_list("name", flat=True)),
            ["Reading guides", "Genre deep dives", "Book explainers", "Author spotlights", "Folklore & myth", "Women writers"],
        )

    def test_topic_filter_and_card_fields(self):
        story = Story.objects.create(title="Linked Book", slug="linked-book", is_published=True)
        a = self._blog("Guide post", topics=[self.guides], days_old=1)
        a.linked_stories.add(story)
        self._blog("Folk post", topics=[self.folk, self.guides], days_old=2)
        self._blog("Untagged post", days_old=3)
        self.assertEqual(self._titles(topic="reading-guides"), ["Guide post", "Folk post"])
        self.assertEqual(self._titles(topic="folklore-myth"), ["Folk post"])
        self.assertEqual(self._titles(topic="nope"), [])
        row = self._list(topic="reading-guides")["results"][0]
        self.assertEqual(row["topics"], [{"name": "Reading guides", "slug": "reading-guides"}])
        self.assertEqual(row["linked_story"], {"title": "Linked Book", "slug": "linked-book"})
        self.assertEqual(row["reading_time_minutes"], 1)
        self.assertNotIn("content", row)

    def test_search_covers_title_and_excerpt(self):
        self._blog("Dragons of the East", excerpt="A tour", days_old=1)
        self._blog("Plain title", excerpt="All about dragon lore", days_old=2)
        self._blog("Unrelated", excerpt="Nothing", days_old=3)
        self.assertEqual(self._titles(q="dragon"), ["Dragons of the East", "Plain title"])
        self.assertEqual(self._titles(q="  UNRELATED "), ["Unrelated"])
        self.assertEqual(self._titles(q="zzz"), [])

    def test_pagination_is_twelve_per_page(self):
        for i in range(13):
            self._blog(f"Post {i:02d}", days_old=i)
        first = self._list()
        self.assertEqual((first["pagination"]["count"], len(first["results"])), (13, 12))
        self.assertEqual(first["pagination"]["pages"], 2)
        second = self._list(page=2)
        self.assertEqual([r["title"] for r in second["results"]], ["Post 12"])

    def test_drafts_and_scheduled_posts_are_not_listed(self):
        self._blog("Live")
        self._blog("Draft", is_published=False)
        self._blog("Later", publish_at=self.now + timedelta(days=2))
        self.assertEqual(self._titles(), ["Live"])

    def test_old_linked_filter_still_works(self):
        story = Story.objects.create(title="B", slug="b-book", is_published=True)
        self._blog("With story").linked_stories.add(story)
        self._blog("General")
        self.assertEqual(self._titles(linked_to_story="true"), ["With story"])
        self.assertEqual(self._titles(linked_to_story="false"), ["General"])
        self.assertEqual(self._titles(sort="oldest"), ["With story", "General"])

    def test_list_query_count_does_not_grow_with_posts(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._blog("One", topics=[self.guides])
        cache.clear()
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("blog-list"))
        for i in range(8):
            blog = self._blog(f"More {i}", topics=[self.guides, self.folk])
            blog.linked_stories.add(Story.objects.create(title=f"S{i}", slug=f"s-{i}", is_published=True))
        cache.clear()
        with CaptureQueriesContext(connection) as big:
            self.client.get(reverse("blog-list"))
        self.assertEqual(len(big), len(small))

    # --- popular ------------------------------------------------------------------------

    def test_popular_sort_uses_recent_unique_audience_readers(self):
        quiet = self._blog("Quiet but newest", days_old=0)
        hit = self._blog("The hit", days_old=20)
        steady = self._blog("Steady", days_old=10)
        old_fame = self._blog("Old fame", days_old=40)
        for i in range(5):
            self._read(hit, f"h{i}")
        self._read(hit, "h0", "reading_session")  # same person twice
        for i in range(3):
            self._read(steady, f"s{i}")
        for i in range(30):
            self._read(old_fame, f"o{i}", days_ago=45)  # outside the window
        self._read(quiet, "staff", user=self.staff)
        for i in range(20):
            self._read(quiet, f"bot{i}", is_suspected_bot=True)
        data = self._list(sort="popular")
        self.assertEqual([r["title"] for r in data["results"]], ["The hit", "Steady", "Quiet but newest", "Old fame"])
        self.assertEqual([r["reads_30d"] for r in data["results"]], [5, 3, 0, 0])

    def test_popular_sort_breaks_ties_by_newest(self):
        older, newer = self._blog("Older", days_old=5), self._blog("Newer", days_old=1)
        self._read(older, "a")
        self._read(newer, "b")
        self.assertEqual(self._titles(sort="popular"), ["Newer", "Older"])

    # --- reading time -------------------------------------------------------------------

    def test_reading_time_is_words_at_230_wpm_minimum_one(self):
        from apps.story.models import blog_reading_minutes

        words = lambda n: "<p>" + " ".join(["word"] * n) + "</p>"  # noqa: E731
        self.assertEqual(blog_reading_minutes(""), 1)
        self.assertEqual(blog_reading_minutes(words(10)), 1)
        self.assertEqual(blog_reading_minutes(words(230)), 1)
        self.assertEqual(blog_reading_minutes(words(345)), 2)  # 1.5 rounds up
        self.assertEqual(blog_reading_minutes(words(2300)), 10)
        self.assertEqual(blog_reading_minutes("<p>" + "<b>x</b> " * 460 + "</p>"), 2)  # markup is not words

    def test_saving_a_post_sets_and_refreshes_its_reading_time(self):
        blog = self._blog("Timed", content="<p>" + " ".join(["w"] * 700) + "</p>")
        blog.refresh_from_db()
        self.assertEqual(blog.reading_time_minutes, 3)
        blog.content = "<p>" + " ".join(["w"] * 2300) + "</p>"
        blog.save(update_fields=["content"])
        blog.refresh_from_db()
        self.assertEqual(blog.reading_time_minutes, 10)

    # --- overview -------------------------------------------------------------------------

    def _overview(self):
        cache.clear()
        response = self.client.get(reverse("blog-overview"))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_overview_payload_shape(self):
        story = Story.objects.create(title="Book A", slug="book-a", is_published=True, cover_image="https://x/c.jpg")
        posts = [self._blog(f"P{i}", topics=[self.guides], days_old=i) for i in range(6)]
        for post in posts[:2]:
            post.linked_stories.add(story)
        for i, post in enumerate(posts):
            for r in range(i + 1):
                self._read(post, f"{post.slug}-{r}")
        data = self._overview()
        self.assertEqual(set(data), {"featured", "featured_is_pinned", "most_read", "topics", "total_posts", "linked_books"})
        self.assertEqual(data["total_posts"], 6)
        self.assertEqual(len(data["most_read"]), 4)
        self.assertEqual([r["title"] for r in data["most_read"]], ["P5", "P4", "P3", "P2"])
        self.assertEqual(data["most_read"][0]["reads_30d"], 6)
        self.assertEqual({t["slug"]: t["post_count"] for t in data["topics"]}["reading-guides"], 6)
        self.assertEqual(len(data["topics"]), 6)
        self.assertEqual(
            [(b["slug"], b["post_count"]) for b in data["linked_books"]], [("book-a", 2)]
        )
        self.assertEqual(data["linked_books"][0]["cover_image"], "https://x/c.jpg")

    def test_featured_falls_back_to_the_newest_post_and_can_be_pinned(self):
        self._blog("Old", days_old=9)
        newest = self._blog("Newest", days_old=0)
        pinned = self._blog("Pinned old one", days_old=30, is_featured=True)
        self._blog("Draft newest", is_published=False)
        pinned_view = self._overview()
        self.assertEqual(pinned_view["featured"]["title"], "Pinned old one")
        self.assertTrue(pinned_view["featured_is_pinned"])
        Blog.objects.filter(pk=pinned.pk).update(is_featured=False)
        fallback = self._overview()
        self.assertEqual(fallback["featured"]["title"], "Newest")
        self.assertFalse(fallback["featured_is_pinned"])
        self.assertIsNotNone(newest.pk)

    def test_featured_is_unique_and_unpublished_pin_is_ignored(self):
        a = self._blog("A", is_featured=True)
        b = self._blog("B", is_featured=True)
        a.refresh_from_db()
        self.assertFalse(a.is_featured)
        self.assertTrue(b.is_featured)
        Blog.objects.filter(pk=b.pk).update(is_published=False)
        self.assertEqual(self._overview()["featured"]["title"], "A")

    def test_overview_with_no_posts(self):
        data = self._overview()
        self.assertIsNone(data["featured"])
        self.assertEqual((data["most_read"], data["linked_books"], data["total_posts"]), ([], [], 0))

    def test_overview_is_cached_for_fifteen_minutes(self):
        self._blog("First")
        self._overview()
        self._blog("Second newest")
        again = self.client.get(reverse("blog-overview")).json()
        self.assertEqual(again["total_posts"], 1)

    # --- admin ----------------------------------------------------------------------------------

    def test_admin_edits_topics_and_reading_time_is_read_only(self):
        blog = self._blog("Editable", content="<p>" + " ".join(["w"] * 460) + "</p>")
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            reverse("admin-blog-detail", args=[blog.pk]),
            {"topics": [self.guides.pk, self.folk.pk], "reading_time_minutes": 99, "is_featured": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual([t["slug"] for t in data["topic_details"]], ["reading-guides", "folklore-myth"])
        self.assertEqual(data["reading_time_minutes"], 2)
        self.assertTrue(data["is_featured"])
        cleared = self.client.patch(reverse("admin-blog-detail", args=[blog.pk]), {"topics": []}, format="json")
        self.assertEqual(cleared.json()["topic_details"], [])

    def test_admin_topics_endpoint_lists_counts_and_needs_superuser(self):
        self._blog("Tagged", topics=[self.guides])
        self.assertEqual(self.client.get(reverse("admin-blog-topic-list")).status_code, 401)
        self.client.force_authenticate(self.admin)
        rows = self.client.get(reverse("admin-blog-topic-list")).json()
        self.assertEqual({r["slug"]: r["post_count"] for r in rows}["reading-guides"], 1)

    def test_suggest_topics_action_queues_a_job(self):
        blog = self._blog("To label")
        self.client.force_authenticate(self.admin)
        with patch("apps.story.api.ai_generation_executor") as executor:
            response = self.client.post(reverse("admin-blog-suggest-topics", args=[blog.pk]))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["topics_status"], "pending")

    # --- backfill commands ----------------------------------------------------------------------

    def test_suggest_blog_topics_is_a_dry_run_by_default(self):
        from django.core.management import call_command

        blog = self._blog("Five Folktales to Read This Monsoon", excerpt="Rain, rivers and folk tales, a reading list.")
        out = io.StringIO()
        call_command("suggest_blog_topics", "--heuristic", stdout=out)
        self.assertEqual(blog.topics.count(), 0)
        text = out.getvalue()
        self.assertIn("DRY RUN", text)
        self.assertIn("Five Folktales to Read This Monsoon", text)
        self.assertIn("Folklore & myth", text)
        self.assertIn("Nothing was written", text)

        call_command("suggest_blog_topics", "--heuristic", "--apply", stdout=io.StringIO())
        blog.refresh_from_db()
        self.assertIn("folklore-myth", list(blog.topics.values_list("slug", flat=True)))
        self.assertEqual(blog.topics_status, "completed")

    def test_suggest_blog_topics_only_uses_the_seeded_list_and_skips_tagged_posts(self):
        from django.core.management import call_command
        from apps.story.blog_topics import parse_topic_slugs

        self.assertEqual(parse_topic_slugs("Folklore-Myth, made-up, reading-guides, women-writers", {"folklore-myth", "reading-guides", "women-writers"}), ["folklore-myth", "reading-guides"])
        tagged = self._blog("Already tagged", topics=[self.guides])
        out = io.StringIO()
        call_command("suggest_blog_topics", "--heuristic", stdout=out)
        self.assertNotIn("Already tagged", out.getvalue())
        self.assertEqual(list(tagged.topics.all()), [self.guides])

    def test_suggest_blog_topics_without_a_key_refuses_ai_mode(self):
        from django.core.management import CommandError, call_command

        self._blog("Needs a key")
        with self.settings(ANTHROPIC_API_KEY=""):
            with self.assertRaises(CommandError):
                call_command("suggest_blog_topics", stdout=io.StringIO())

    def test_queue_missing_excerpts_is_a_dry_run_by_default_and_reports_the_count(self):
        from django.core.management import call_command

        self._blog("No excerpt")
        self._blog("Blank excerpt", excerpt="")
        self._blog("Has one", excerpt="Already written")
        self._blog("Draft", is_published=False)
        with patch("apps.story.management.commands.queue_missing_blog_excerpts.executor") as executor:
            out = io.StringIO()
            call_command("queue_missing_blog_excerpts", stdout=out)
            executor.submit.assert_not_called()
            self.assertIn("2 published post(s) have no excerpt", out.getvalue())
            applied = io.StringIO()
            call_command("queue_missing_blog_excerpts", "--apply", stdout=applied)
            self.assertEqual(executor.submit.call_count, 2)
            self.assertIn("Queued 2 excerpt generation job(s)", applied.getvalue())


class StoryAdminOverviewApiTests(APITestCase):
    """The admin Stories page: status / cover / chapter filters, sort, and the stats counts."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="stories-admin@example.com", username="stories-admin",
            password="test-password", is_superuser=True, is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.now = timezone.now()

        def make(slug, **kw):
            return Story.objects.create(title=slug.replace("-", " ").title(), slug=slug, **kw)

        # live, complete: cover, summary, retrospective, chapter
        self.full = make("full", is_published=True, cover_image="https://x/c.jpg", summary="<p>S</p>", retrospective="<p>R</p>")
        Chapter.objects.create(story=self.full, title="C", slug="full-c", content="<p>x</p>", order=1)
        # live, missing everything
        self.bare = make("bare", is_published=True)
        # live, missing only the cover
        self.nocover = make("no-cover", is_published=True, summary="<p>S</p>", retrospective="<p>R</p>")
        Chapter.objects.create(story=self.nocover, title="C", slug="nc-c", content="<p>x</p>", order=1)
        self.draft = make("draft-one", is_published=False)
        self.scheduled = make("scheduled-one", is_published=True, publish_at=self.now + timedelta(days=3))
        self.passed = make("passed-one", is_published=True, publish_at=self.now - timedelta(days=3), summary="<p>S</p>")

    def _slugs(self, **params):
        # The viewset's own queryset (filters + ordering). Serializing the whole list is skipped
        # here: the admin list serializer cannot render under this project's Python 3.15 test
        # environment (the same reason StoryAdminListFilterTests errors), and the filters are what is under test.
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from apps.story.api import StoryAdminViewSet

        view = StoryAdminViewSet()
        view.request = Request(APIRequestFactory().get("/api/admin/stories/", params))
        view.action = "list"
        view.format_kwarg = None
        return [story.slug for story in view.filter_queryset(view.get_queryset())]

    def test_status_filter_matches_the_badge_definitions(self):
        self.assertEqual(set(self._slugs(status="draft")), {"draft-one"})
        self.assertEqual(set(self._slugs(status="scheduled")), {"scheduled-one"})
        self.assertEqual(set(self._slugs(status="published")), {"full", "bare", "no-cover", "passed-one"})
        self.assertEqual(len(self._slugs()), 6)
        self.assertEqual(len(self._slugs(status="bogus")), 6)

    def test_has_cover_filter(self):
        self.assertEqual(set(self._slugs(has_cover="true")), {"full"})
        self.assertEqual(set(self._slugs(has_cover="false")), {"bare", "no-cover", "draft-one", "scheduled-one", "passed-one"})
        self.assertEqual(len(self._slugs(has_cover="maybe")), 6)

    def test_has_chapters_filter(self):
        self.assertEqual(set(self._slugs(has_chapters="true")), {"full", "no-cover"})
        self.assertEqual(set(self._slugs(has_chapters="false")), {"bare", "draft-one", "scheduled-one", "passed-one"})

    def test_filters_combine_for_a_needs_attention_view(self):
        self.assertEqual(set(self._slugs(status="published", has_cover="false")), {"bare", "no-cover", "passed-one"})
        self.assertEqual(set(self._slugs(status="published", has_cover="false", has_chapters="false")), {"bare", "passed-one"})
        self.assertEqual(set(self._slugs(status="published", has_summary="false")), {"bare"})

    def test_sort_is_whitelisted_and_defaults_to_newest(self):
        newest = self._slugs()
        self.assertEqual(newest[0], "passed-one")  # highest id first, same as before
        self.assertEqual(self._slugs(sort="newest"), newest)
        self.assertEqual(self._slugs(sort="oldest"), list(reversed(newest)))
        titles = self._slugs(sort="title_asc")
        self.assertEqual(titles, sorted(titles))
        self.assertEqual(self._slugs(sort="title_desc"), list(reversed(titles)))
        self.assertEqual(self._slugs(sort="id; drop table"), newest)  # unknown values are ignored

    def test_list_renders_and_its_query_count_does_not_grow_with_stories(self):
        url = reverse("admin-story-list")
        self.assertEqual(self.client.get(url, format="json").status_code, 200)
        Story.objects.filter(pk=self.full.pk).update(site_published_date=timezone.now().date())
        row = next(r for r in self.client.get(url, format="json").json()["results"] if r["slug"] == "full")
        self.assertEqual((row["chapter_count"], row["audio_count"], row["video_count"]), (1, 0, 0))
        self.assertRegex(row["published_date_label"], r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")  # no zero-padding trick needed
        with CaptureQueriesContext(connection) as before:
            self.client.get(url, format="json")
        for i in range(8):
            extra = Story.objects.create(title=f"Bulk {i}", slug=f"bulk-{i}", is_published=True)
            Chapter.objects.create(story=extra, title="C", slug=f"bulk-c-{i}", content="<p>x</p>", order=1)
        with CaptureQueriesContext(connection) as after:
            self.client.get(url, format="json")
        self.assertEqual(len(after), len(before))

    def test_stats_counts(self):
        response = self.client.get(reverse("admin-story-stats"), format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(),
            {
                "total": 6,
                "published": 4,
                "draft": 1,
                "scheduled": 1,
                # among published (live): bare, no-cover, passed-one lack a cover; bare lacks a summary
                # and a chapter; passed-one lacks a chapter; bare lacks a retrospective, as does passed-one.
                "missing_summary": 1,
                "missing_retrospective": 2,
                "missing_cover": 3,
                "missing_chapters": 2,
            },
        )

    def test_stats_agree_with_the_list_filters_and_need_a_superuser(self):
        stats = self.client.get(reverse("admin-story-stats"), format="json").json()
        self.assertEqual(stats["missing_cover"], len(self._slugs(status="published", has_cover="false")))
        self.assertEqual(stats["missing_chapters"], len(self._slugs(status="published", has_chapters="false")))
        self.assertEqual(stats["missing_summary"], len(self._slugs(status="published", has_summary="false")))
        self.assertEqual(stats["missing_retrospective"], len(self._slugs(status="published", has_retrospective="false")))
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse("admin-story-stats")).status_code, 401)
        member = User.objects.create_user(email="m@example.com", username="m", password="x")
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get(reverse("admin-story-stats")).status_code, 403)

    def test_stats_query_count_does_not_grow_with_stories(self):
        with CaptureQueriesContext(connection) as before:
            self.client.get(reverse("admin-story-stats"))
        for i in range(10):
            Story.objects.create(title=f"Extra {i}", slug=f"extra-{i}", is_published=True)
        with CaptureQueriesContext(connection) as after:
            self.client.get(reverse("admin-story-stats"))
        self.assertEqual(len(after), len(before))


class AudiobooksApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.short_story = Story.objects.create(title="Short Audio", slug="short-audio", is_published=True)
        Audio.objects.create(
            story=self.short_story, title="Part 1", slug="short-p1", order=1,
            audio_file="audios/short1.mp3", duration_seconds=610.0,
        )
        Audio.objects.create(
            story=self.short_story, title="Part 2", slug="short-p2", order=2,
            audio_file="audios/short2.mp3", duration_seconds=600.0,
        )  # total = 1210s -> 21 min

        self.medium_story = Story.objects.create(title="Medium Audio", slug="medium-audio", is_published=True)
        Audio.objects.create(
            story=self.medium_story, title="Part 1", slug="med-p1", order=1,
            audio_file="audios/med1.mp3", duration_seconds=3600.0,
        )  # 60 min

        self.long_story = Story.objects.create(title="Long Audio", slug="long-audio", is_published=True)
        Audio.objects.create(
            story=self.long_story, title="Part 1", slug="long-p1", order=1,
            audio_file="audios/long1.mp3", duration_seconds=9000.0,
        )  # 150 min

        self.epic_story = Story.objects.create(title="Epic Audio", slug="epic-audio", is_published=True)
        Audio.objects.create(
            story=self.epic_story, title="Part 1", slug="epic-p1", order=1,
            audio_file="audios/epic1.mp3", duration_seconds=18600.0,
        )  # 310 min

        self.unknown_story = Story.objects.create(title="Unknown Audio", slug="unknown-audio", is_published=True)
        Audio.objects.create(
            story=self.unknown_story, title="Part 1", slug="unk-p1", order=1,
            audio_file="audios/unk1.mp3", duration_seconds=None,
        )

        # Draft story and audio without file should not count in stats or public list
        self.draft_story = Story.objects.create(title="Draft Audio", slug="draft-audio", is_published=False)
        Audio.objects.create(
            story=self.draft_story, title="Part 1", slug="draft-p1", order=1,
            audio_file="audios/draft1.mp3", duration_seconds=500.0,
        )
        self.no_file_story = Story.objects.create(title="No File Audio", slug="no-file-audio", is_published=True)
        Audio.objects.create(
            story=self.no_file_story, title="Part 1", slug="nofile-p1", order=1,
            audio_file="", duration_seconds=900.0,
        )

    def test_audio_length_buckets_and_max_audio_minutes(self):
        short_resp = self.client.get("/api/stories/?has_audio=true&audio_length=short")
        self.assertEqual(short_resp.status_code, 200)
        self.assertEqual([r["slug"] for r in short_resp.data["results"]], ["short-audio"])

        med_resp = self.client.get("/api/stories/?has_audio=true&audio_length=medium")
        self.assertEqual([r["slug"] for r in med_resp.data["results"]], ["medium-audio"])

        long_resp = self.client.get("/api/stories/?has_audio=true&audio_length=long")
        self.assertEqual([r["slug"] for r in long_resp.data["results"]], ["long-audio"])

        epic_resp = self.client.get("/api/stories/?has_audio=true&audio_length=epic")
        self.assertEqual([r["slug"] for r in epic_resp.data["results"]], ["epic-audio"])

        all_resp = self.client.get("/api/stories/?has_audio=true&audio_length=all")
        self.assertIn("unknown-audio", [r["slug"] for r in all_resp.data["results"]])

        max_resp = self.client.get("/api/stories/?has_audio=true&max_audio_minutes=60")
        self.assertEqual(
            {r["slug"] for r in max_resp.data["results"]},
            {"short-audio", "medium-audio"},
        )

    def test_audio_shortest_and_audio_longest_sort_nulls_last(self):
        shortest = self.client.get("/api/stories/?has_audio=true&sort=audio_shortest")
        self.assertEqual(shortest.status_code, 200)
        slugs_asc = [r["slug"] for r in shortest.data["results"]]
        self.assertEqual(
            slugs_asc[:4],
            ["short-audio", "medium-audio", "long-audio", "epic-audio"],
        )
        self.assertIn("unknown-audio", slugs_asc[4:])

        longest = self.client.get("/api/stories/?has_audio=true&sort=audio_longest")
        self.assertEqual(longest.status_code, 200)
        slugs_desc = [r["slug"] for r in longest.data["results"]]
        self.assertEqual(
            slugs_desc[:4],
            ["epic-audio", "long-audio", "medium-audio", "short-audio"],
        )
        self.assertIn("unknown-audio", slugs_desc[4:])

    def test_audio_minutes_field_values_and_null(self):
        resp = self.client.get("/api/stories/?has_audio=true")
        self.assertEqual(resp.status_code, 200)
        by_slug = {r["slug"]: r["audio_minutes"] for r in resp.data["results"]}
        self.assertEqual(by_slug["short-audio"], 21)
        self.assertEqual(by_slug["medium-audio"], 60)
        self.assertEqual(by_slug["long-audio"], 150)
        self.assertEqual(by_slug["epic-audio"], 310)
        self.assertIsNone(by_slug["unknown-audio"])

    def test_audiobook_stats_endpoint(self):
        resp = self.client.get("/api/stories/audiobook-stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.data,
            {
                "count": 5,
                "total_seconds": 1210 + 3600 + 9000 + 18600,
                "unknown_duration_count": 1,
            },
        )

    def test_story_detail_audio_duration_seconds(self):
        resp_known = self.client.get(f"/api/stories/{self.short_story.slug}/")
        self.assertEqual(resp_known.status_code, 200)
        durations = {a["slug"]: a["duration_seconds"] for a in resp_known.data["audios"]}
        self.assertEqual(durations["short-p1"], 610.0)
        self.assertEqual(durations["short-p2"], 600.0)

        resp_unknown = self.client.get(f"/api/stories/{self.unknown_story.slug}/")
        self.assertEqual(resp_unknown.status_code, 200)
        self.assertIsNone(resp_unknown.data["audios"][0]["duration_seconds"])


class QuickReadsApiTests(APITestCase):
    def setUp(self):
        from apps.stats.models import QuickReadProgress
        from django.core.management import call_command

        self.QuickReadProgress = QuickReadProgress
        self.call_command = call_command
        self.user = User.objects.create_user(
            email="qr@example.com", username="qrreader", password="password123"
        )
        # reading_time.summary_reading_minutes uses ~200 wpm (min 1 min)
        self.s_short = Story.objects.create(
            title="Short Summary",
            slug="short-summary",
            is_published=True,
            summary="<p>" + " ".join(["word"] * 250) + "</p>",  # 2 min
        )
        self.s_mid = Story.objects.create(
            title="Mid Summary",
            slug="mid-summary",
            is_published=True,
            summary="<p>" + " ".join(["word"] * 800) + "</p>",  # 4 min
        )
        self.s_long = Story.objects.create(
            title="Long Summary",
            slug="long-summary",
            is_published=True,
            summary="<p>" + " ".join(["word"] * 1500) + "</p>",  # 8 min
        )
        self.s_null = Story.objects.create(
            title="Null Cached Summary",
            slug="null-cached-summary",
            is_published=True,
            summary="<p>Some summary text</p>",
        )
        Story.objects.filter(pk=self.s_null.pk).update(cached_summary_reading_minutes=None)
        self.s_null.refresh_from_db()

    def test_cached_field_updates_on_save_and_update_fields(self):
        from apps.story import reading_time

        self.assertEqual(
            self.s_short.cached_summary_reading_minutes,
            reading_time.summary_reading_minutes(self.s_short.summary),
        )
        self.assertLess(self.s_short.cached_summary_reading_minutes, 3)
        self.assertTrue(3 <= self.s_mid.cached_summary_reading_minutes <= 5)
        self.assertGreater(self.s_long.cached_summary_reading_minutes, 5)

        self.s_short.summary = "<p>" + " ".join(["word"] * 1000) + "</p>"
        self.s_short.save(update_fields=["summary"])
        self.s_short.refresh_from_db()
        self.assertEqual(
            self.s_short.cached_summary_reading_minutes,
            reading_time.summary_reading_minutes(self.s_short.summary),
        )

        self.s_short.summary = ""
        self.s_short.save(update_fields=["summary"])
        self.s_short.refresh_from_db()
        self.assertIsNone(self.s_short.cached_summary_reading_minutes)

    def test_backfill_summary_minutes_is_idempotent(self):
        from io import StringIO

        self.assertIsNone(self.s_null.cached_summary_reading_minutes)
        out1 = StringIO()
        self.call_command("backfill_summary_minutes", stdout=out1)
        self.s_null.refresh_from_db()
        self.assertEqual(self.s_null.cached_summary_reading_minutes, 1)
        self.assertIn("1 stories", out1.getvalue())

        out2 = StringIO()
        self.call_command("backfill_summary_minutes", stdout=out2)
        self.assertIn("0 stories", out2.getvalue())

    def test_summary_length_buckets_exclude_nulls(self):
        under3 = self.client.get("/api/stories/?has_summary=true&summary_length=under3")
        self.assertEqual([r["slug"] for r in under3.data["results"]], ["short-summary"])

        mid = self.client.get("/api/stories/?has_summary=true&summary_length=mid")
        self.assertEqual([r["slug"] for r in mid.data["results"]], ["mid-summary"])

        long_res = self.client.get("/api/stories/?has_summary=true&summary_length=long")
        self.assertEqual([r["slug"] for r in long_res.data["results"]], ["long-summary"])

        all_res = self.client.get("/api/stories/?has_summary=true&summary_length=all")
        self.assertIn("null-cached-summary", [r["slug"] for r in all_res.data["results"]])

    def test_summary_shortest_and_longest_sort_nulls_last(self):
        asc = self.client.get("/api/stories/?has_summary=true&sort=summary_shortest")
        self.assertEqual(
            [r["slug"] for r in asc.data["results"]],
            ["short-summary", "mid-summary", "long-summary", "null-cached-summary"],
        )

        desc = self.client.get("/api/stories/?has_summary=true&sort=summary_longest")
        self.assertEqual(
            [r["slug"] for r in desc.data["results"]],
            ["long-summary", "mid-summary", "short-summary", "null-cached-summary"],
        )

    def test_not_read_quick_and_quick_read_progress_field(self):
        self.QuickReadProgress.objects.create(user=self.user, story=self.s_short, progress=0.95)
        self.QuickReadProgress.objects.create(user=self.user, story=self.s_mid, progress=0.45)

        anon = self.client.get("/api/stories/?has_summary=true&not_read_quick=true")
        self.assertIn("short-summary", [r["slug"] for r in anon.data["results"]])
        for row in anon.data["results"]:
            self.assertIsNone(row["quick_read_progress"])

        self.client.force_authenticate(user=self.user)
        authed = self.client.get("/api/stories/?has_summary=true&not_read_quick=true")
        slugs = [r["slug"] for r in authed.data["results"]]
        self.assertNotIn("short-summary", slugs)
        self.assertIn("mid-summary", slugs)
        by_slug = {r["slug"]: r["quick_read_progress"] for r in authed.data["results"]}
        self.assertAlmostEqual(by_slug["mid-summary"], 0.45)
        self.assertIsNone(by_slug["long-summary"])

    def test_quick_read_hero_continue_pick_none_and_public_access(self):
        # Anonymous list + detail with summary are public and return 200
        anon_list = self.client.get("/api/stories/?has_summary=true")
        self.assertEqual(anon_list.status_code, 200)
        self.assertGreaterEqual(len(anon_list.data["results"]), 4)
        for row in anon_list.data["results"]:
            self.assertIsNone(row["quick_read_progress"])

        anon_detail = self.client.get(f"/api/stories/{self.s_mid.slug}/")
        self.assertEqual(anon_detail.status_code, 200)
        self.assertTrue(anon_detail.data.get("summary"))

        # Anonymous hero returns deterministic daily pick (never continue)
        anon_hero = self.client.get("/api/stories/quick-read-hero/")
        self.assertEqual(anon_hero.status_code, 200)
        self.assertEqual(anon_hero.data["mode"], "pick")
        self.assertIn("slug", anon_hero.data["story"])

        self.client.force_authenticate(user=self.user)
        # No progress yet -> deterministic daily pick
        pick_resp = self.client.get("/api/stories/quick-read-hero/")
        self.assertEqual(pick_resp.status_code, 200)
        self.assertEqual(pick_resp.data["mode"], "pick")
        self.assertIn("slug", pick_resp.data["story"])

        # Add in-progress -> mode continue for authenticated user, still pick for anonymous
        self.QuickReadProgress.objects.create(user=self.user, story=self.s_mid, progress=0.4)
        cont_resp = self.client.get("/api/stories/quick-read-hero/")
        self.assertEqual(cont_resp.status_code, 200)
        self.assertEqual(cont_resp.data["mode"], "continue")
        self.assertEqual(cont_resp.data["story"]["slug"], "mid-summary")
        self.assertAlmostEqual(cont_resp.data["progress"], 0.4)

        self.client.force_authenticate(user=None)
        anon_hero_after = self.client.get("/api/stories/quick-read-hero/")
        self.assertEqual(anon_hero_after.status_code, 200)
        self.assertEqual(anon_hero_after.data["mode"], "pick")

        # Delete all summaries -> mode none
        Story.objects.all().delete()
        none_resp = self.client.get("/api/stories/quick-read-hero/")
        self.assertEqual(none_resp.status_code, 200)
        self.assertEqual(none_resp.data, {"mode": "none"})

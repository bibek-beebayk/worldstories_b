import csv
import io
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from ebooklib import epub
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from storages.backends.s3 import S3Storage

import anthropic
import openpyxl
from pydantic import ValidationError as PydanticValidationError

from apps.story.api import StoryMapAPIView, StoryViewSet, open_s3_audio_stream
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
from apps.story.queue_import import (
    MAX_IMPORT_ROWS,
    ImportFileError,
    build_preview,
    confirm_import,
    parse_uploaded_file,
)
from apps.story.epub_import import EpubParseError, extract_chapters
from apps.story.excerpts import _excerpt_from_text
from apps.story.epub_import_jobs import run_epub_import
from apps.story.models import (
    Audio,
    Author,
    Blog,
    BookFetchJob,
    Category,
    Chapter,
    EpubImportJob,
    Favorite,
    Genre,
    PromptSettings,
    Story,
    StoryQueue,
)
from apps.story.serializers import AudioAdminSerializer, StoryAdminSerializer, SubmissionSerializer
from apps.story import reading_time
from apps.users.models import User
from core.urls import sitemap


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


class StoryMapDataTests(SimpleTestCase):
    @patch("apps.story.api.Story.objects.published")
    def test_story_map_groups_only_published_stories_with_countries(self, published):
        grouped = [
            {"country": "JP", "stories_count": 3},
            {"country": "NP", "stories_count": 1},
        ]
        published.return_value.exclude.return_value.values.return_value.annotate.return_value.order_by.return_value = grouped

        response = StoryMapAPIView().get(RequestFactory().get("/api/story-map/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_stories"], 4)
        self.assertEqual(response.data["countries_count"], 2)
        self.assertEqual(response.data["max_stories_count"], 3)
        self.assertEqual(
            response.data["countries"],
            [
                {"code": "JP", "name": "Japan", "stories_count": 3},
                {"code": "NP", "name": "Nepal", "stories_count": 1},
            ],
        )
        published.return_value.exclude.assert_called_once_with(country="")


class ScheduledPublishingTests(SimpleTestCase):
    @patch("apps.story.api.Story.objects.published")
    def test_public_queryset_is_built_for_each_request(self, published):
        queryset = MagicMock()
        queryset.select_related.return_value.order_by.return_value = queryset
        published.return_value = queryset
        view = StoryViewSet()
        view.action = "retrieve"

        self.assertIs(view.get_queryset(), queryset)
        published.assert_called_once_with()
        queryset.select_related.assert_called_once_with("author")

    @patch("django.db.models.Model.save")
    def test_scheduled_story_uses_schedule_date_as_site_date(self, model_save):
        story = Story(
            title="Scheduled",
            slug="scheduled",
            is_published=True,
            publish_at=datetime(2026, 9, 4, 10, 0, tzinfo=datetime_timezone.utc),
        )

        story.save()

        self.assertEqual(story.site_published_date, date(2026, 9, 4))
        model_save.assert_called_once()

    @patch("core.urls.Author.objects.all")
    @patch("core.urls.Blog.objects.published")
    @patch("core.urls.Story.objects.published")
    def test_sitemap_uses_scheduled_publication_gate(self, published, blogs_published, authors_all):
        chapter = SimpleNamespace(slug="chapter-one")
        story = SimpleNamespace(
            slug="visible-story",
            site_published_date=date(2026, 8, 2),
            chapters=SimpleNamespace(all=lambda: [chapter]),
        )
        queryset = MagicMock()
        (
            queryset.exclude.return_value.prefetch_related.return_value.only.return_value.iterator.return_value
        ) = iter([story])
        published.return_value = queryset
        blogs_published.return_value.only.return_value.iterator.return_value = iter([])
        authors_all.return_value.only.return_value.iterator.return_value = iter([])

        response = sitemap(RequestFactory().get("/api/sitemap.xml"))
        xml = response.content.decode()

        self.assertContains(response, "/story/visible-story")
        self.assertContains(response, "/read/visible-story/chapter-one")
        self.assertContains(response, "/story-map")
        self.assertIn("<lastmod>2026-08-02</lastmod>", xml)
        published.assert_called_once_with()
        queryset.exclude.assert_called_once_with(story_type="Summary")


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

    def test_linked_story_survives_but_nulls_when_story_deleted(self):
        story = Story.objects.create(title="Book", slug="book")
        blog = Blog.objects.create(title="Post", slug="post", content="<p>x</p>", linked_story=story)

        story.delete()
        blog.refresh_from_db()

        self.assertIsNone(blog.linked_story)

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
                "linked_story": story.id,
                "cover_image_file": image,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["linked_story"], story.id)
        self.assertEqual(response.data["linked_story_detail"]["slug"], "linked-book")
        self.assertTrue(response.data["cover_image_url"])

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
        Blog.objects.create(title="Linked", slug="linked", content="<p>x</p>", linked_story=story)
        Blog.objects.create(title="General", slug="general", content="<p>x</p>")

        linked_response = self.client.get("/api/blog/?linked_to_story=true")
        general_response = self.client.get("/api/blog/?linked_to_story=false")

        self.assertEqual([i["slug"] for i in linked_response.data["results"]], ["linked"])
        self.assertEqual([i["slug"] for i in general_response.data["results"]], ["general"])

    def test_linked_story_slug_filter_scopes_to_one_story(self):
        story_a = Story.objects.create(title="Book A", slug="book-a")
        story_b = Story.objects.create(title="Book B", slug="book-b")
        Blog.objects.create(title="For A", slug="for-a", content="<p>x</p>", linked_story=story_a)
        Blog.objects.create(title="For B", slug="for-b", content="<p>x</p>", linked_story=story_b)
        Blog.objects.create(title="Unlinked", slug="unlinked", content="<p>x</p>")

        response = self.client.get("/api/blog/?linked_story=book-a")

        self.assertEqual([i["slug"] for i in response.data["results"]], ["for-a"])

    def test_detail_404s_for_unpublished_slug(self):
        Blog.objects.create(title="Draft", slug="draft", content="<p>x</p>", is_published=False)

        response = self.client.get("/api/blog/draft/")

        self.assertEqual(response.status_code, 404)

    def test_detail_includes_linked_story_summary(self):
        story = Story.objects.create(title="Book Title", slug="book-title")
        Blog.objects.create(title="Post", slug="post", content="<p>x</p>", linked_story=story)

        response = self.client.get("/api/blog/post/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["linked_story"]["slug"], "book-title")
        self.assertEqual(response.data["linked_story"]["title"], "Book Title")

    def test_detail_includes_updated_at_for_date_modified(self):
        Blog.objects.create(title="Post", slug="post", content="<p>x</p>")

        response = self.client.get("/api/blog/post/")

        self.assertTrue(response.data["updated_at"])

    def test_detail_linked_story_null_when_absent(self):
        Blog.objects.create(title="Post", slug="post", content="<p>x</p>")

        response = self.client.get("/api/blog/post/")

        self.assertIsNone(response.data["linked_story"])


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

    def test_list_includes_all_authors_and_only_counts_published_stories(self):
        response = self.client.get(reverse("author-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pagination"]["count"], 2)
        counts = {author["id"]: author["stories_count"] for author in response.data["results"]}
        self.assertEqual(counts[self.visible_author.id], 1)
        self.assertEqual(counts[self.hidden_author.id], 0)

    def test_detail_only_includes_published_stories(self):
        response = self.client.get(reverse("author-detail", args=[self.visible_author.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["stories"]], ["published-book"])

    def test_author_without_public_stories_has_an_empty_book_list(self):
        response = self.client.get(reverse("author-detail", args=[self.hidden_author.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stories_count"], 0)
        self.assertEqual(response.data["stories"], [])

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

    def test_discover_only_returns_genres_with_public_titles(self):
        public_genre = Genre.objects.create(name="Public Genre")
        empty_genre = Genre.objects.create(name="Draft Only Genre")
        Story.objects.get(slug="published-book").genres.add(public_genre)
        Story.objects.get(slug="draft-book").genres.add(empty_genre)

        response = self.client.get(reverse("discover-data"))

        self.assertEqual(response.status_code, 200)
        genres = {genre["name"]: genre["stories_count"] for genre in response.data["genres"]}
        self.assertEqual(genres, {"Public Genre": 1})

    def test_discover_returns_only_story_types_and_languages_with_public_titles(self):
        Story.objects.create(
            title="Published Spanish Novel",
            slug="published-spanish-novel",
            story_type="Novel",
            language="es",
            is_published=True,
        )
        Story.objects.create(
            title="Draft French Poetry",
            slug="draft-french-poetry",
            story_type="Poetry",
            language="fr",
            is_published=False,
        )

        response = self.client.get(reverse("discover-data"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["value"]: item["stories_count"] for item in response.data["story_types"]},
            {"Novel": 1, "Short Story": 1},
        )
        self.assertEqual(
            {item["value"]: item["stories_count"] for item in response.data["languages"]},
            {"en": 1, "es": 1},
        )
        self.assertEqual(
            {"most_viewed", "highest_rated", "most_favorited", "most_discussed"},
            set(response.data).intersection(
                {"most_viewed", "highest_rated", "most_favorited", "most_discussed"}
            ),
        )

    def test_story_list_can_filter_by_story_type(self):
        Story.objects.create(
            title="Published Novel",
            slug="published-novel",
            story_type="Novel",
            is_published=True,
        )

        response = self.client.get(reverse("story-list"), {"story_type": "Novel"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["results"]], ["published-novel"])

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

    def test_requires_superuser(self):
        regular_user = User.objects.create_user(
            email="storyreport-regular@example.com", username="storyreport-regular", password="x"
        )
        self.client.force_authenticate(regular_user)

        response = self.client.get(reverse("admin-story-list"))

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
            story_type="Novel",
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
        self.assertEqual(story.story_type, "Novel")
        self.assertEqual(story.country, "JP")
        self.assertEqual(story.language, "ja")
        self.assertEqual(story.original_published_year, 2002)
        self.assertEqual(story.original_published_month, 9)
        self.assertEqual(story.original_published_day, 12)
        self.assertEqual(story.cover_image, "https://example.com/cover.jpg")
        self.assertEqual(list(story.genres.values_list("id", flat=True)), [genre.id])
        self.assertEqual(list(story.categories.values_list("id", flat=True)), [category.id])

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


class SubmissionFileUploadValidationTests(SimpleTestCase):
    """A logged-in user can submit whatever bytes they like under a
    pdf_file/epub_file field — the model previously had no extension or size
    validation, so a mislabeled or oversized upload would sail straight
    through to public R2 storage. These pin the FileExtensionValidator /
    FileSizeValidator now declared on the model fields."""

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
            books = fetch_books("title,author\n", 2, "instructions")

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
                    fetch_books("title,author\n", 5, "instructions")

    def test_empty_books_list_raises_book_fetch_error(self):
        with patch("apps.story.book_fetch._client") as mock_client:
            mock_client.return_value.messages.parse.return_value = _mock_book_fetch_response([])
            with self.assertRaises(BookFetchError):
                fetch_books("title,author\n", 5, "instructions")

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
                fetch_books("title,author\n", 5, "instructions")


class RunBookFetchTests(TransactionTestCase):
    """Uses TransactionTestCase (not TestCase) because run_book_fetch calls
    transaction.atomic() and connections.close_all() itself, same reasoning
    as RunEpubImportTests."""

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
        self.assertEqual(created.story_type, "Novel")
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
        self.assertEqual(created.story_type, "")
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
                "original_published_year", "original_published_month", "original_published_day",
                "epub_link", "pdf_link", "cover_image_link",
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

        response = self.client.get("/api/admin/stories/export/")

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        row = rows[1]
        self.assertEqual(row[0], "A Book")
        self.assertEqual(row[1], "Some Author")
        self.assertEqual(row[4], "Japan")
        self.assertEqual(row[5], "Japanese")
        self.assertEqual(row[6], "Adventure, Gothic")
        self.assertEqual(row[7], "Classic Literature")
        self.assertEqual(row[8], "1900")

    def test_export_respects_is_published_filter(self):
        self.client.force_authenticate(user=self._make_superuser())
        Story.objects.create(title="Published", slug="published", is_published=True)
        Story.objects.create(title="Draft", slug="draft", is_published=False)

        response = self.client.get("/api/admin/stories/export/", {"is_published": "true"})

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        titles = [row[0] for row in rows[1:]]
        self.assertEqual(titles, ["Published"])

    def test_export_does_not_include_story_queue_entries(self):
        self.client.force_authenticate(user=self._make_superuser())
        StoryQueue.objects.create(title="Not A Real Story", author_name="Someone")

        response = self.client.get("/api/admin/stories/export/")

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len(rows), 1)  # header only

    def test_export_requires_superuser(self):
        regular_user = User.objects.create(email="user@example.com", username="user", is_active=True)
        self.client.force_authenticate(user=regular_user)

        response = self.client.get("/api/admin/stories/export/")

        self.assertEqual(response.status_code, 403)

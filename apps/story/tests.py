from datetime import date, datetime, timezone as datetime_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from storages.backends.s3 import S3Storage

from apps.story.api import StoryViewSet, open_s3_audio_stream
from apps.story.models import Audio, Author, Genre, Story
from apps.story.serializers import AudioAdminSerializer, StoryAdminSerializer
from apps.story import reading_time
from core.urls import sitemap


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
    @patch("core.urls.Story.objects.published")
    def test_sitemap_uses_scheduled_publication_gate(self, published, authors_all):
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
        authors_all.return_value.only.return_value.iterator.return_value = iter([])

        response = sitemap(RequestFactory().get("/api/sitemap.xml"))
        xml = response.content.decode()

        self.assertContains(response, "/story/visible-story")
        self.assertContains(response, "/read/visible-story/chapter-one")
        self.assertIn("<lastmod>2026-08-02</lastmod>", xml)
        published.assert_called_once_with()
        queryset.exclude.assert_called_once_with(story_type="Summary")


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

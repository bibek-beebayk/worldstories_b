import mimetypes
import os
import re
import uuid

from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet
from rest_framework.views import APIView
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Sum, Avg, Count, Exists, F, OuterRef, Prefetch
from django.db.models import Q
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from datetime import timedelta
from storages.backends.s3 import S3Storage
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import BasePermission
from rest_framework.filters import SearchFilter

from apps.story.filters import StoryFilter
from apps.stats.models import ReadingProgress, AudioReadingProgress, VideoWatchProgress
from apps.users.models import User
from apps.users.recommendations import recommend_because_finished
from .models import (
    default_story_type_id,
    Genre,
    Category,
    Story,
    Chapter,
    Audio,
    AudioTranscriptCue,
    Video,
    Author,
    Review,
    Favorite,
    Submission,
    StoryView,
    EpubImportJob,
    BookFetchJob,
    PromptSettings,
    Blog,
    StoryQueue,
    StoryType,
    Tag,
    Theme,
    with_preferred_translation_only,
    published_story_q,
    LANGUAGE_CHOICES,
    COUNTRY_CHOICES,
)
from .epub_import_jobs import executor as epub_import_executor, run_epub_import
from .rich_text import rich_text_has_content
from .transcripts import (
    SUPPORTED_FORMATS,
    TranscriptParseError,
    format_from_filename,
    parse_transcript,
)
from .book_fetch import DEFAULT_BOOK_FETCH_COUNT, MAX_BOOK_FETCH_COUNT
from .book_fetch_jobs import executor as book_fetch_executor, run_book_fetch
from .queue_import import MAX_IMPORT_ROWS, ImportFileError, build_preview, confirm_import
from .taxonomy_bulk_update import build_taxonomy_preview, confirm_taxonomy_update
from .story_export import build_story_export_csv
from .ai_generation_jobs import (
    executor as ai_generation_executor,
    run_generate_blog_excerpt,
    run_generate_field,
)
from .serializers import (
    GenreSerializer,
    GenreDetailSerializer,
    CategorySerializer,
    CategoryDetailSerializer,
    TagSerializer,
    TagDetailSerializer,
    AdminTagSerializer,
    ThemeSerializer,
    ThemeDetailSerializer,
    AdminThemeSerializer,
    StoryTypeSerializer,
    AdminStoryTypeSerializer,
    AuthorSerializer,
    AuthorDetailSerializer,
    AdminGenreSerializer,
    AdminCategorySerializer,
    AdminAuthorSerializer,
    StoryListSerializer,
    FeaturedStorySerializer,
    ChapterSearchResultSerializer,
    StoryDetailSerializer,
    ChapterSerializer,
    AudioSerializer,
    AudioTranscriptCueSerializer,
    ReadAlongSerializer,
    VideoSerializer,
    ReviewSerializer,
    ReviewWriteSerializer,
    SubmissionSerializer,
    SubmissionListSerializer,
    StoryAdminSerializer,
    ChapterAdminSerializer,
    AudioAdminSerializer,
    VideoAdminSerializer,
    SubmissionAdminSerializer,
    EpubImportJobSerializer,
    BookFetchJobSerializer,
    PromptSettingsSerializer,
    BlogSerializer,
    BlogAdminSerializer,
    StoryQueueSerializer,
)
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError
from core.libs.pagination import PageNumberPagination


VIEW_DEDUPE_WINDOW = timedelta(hours=24)

# Same bot list the social-meta Netlify edge function checks — its server-side fetches
# to this endpoint (for link-preview scraping) shouldn't be counted as real reads.
BOT_USER_AGENT_PATTERN = re.compile(
    r"facebookexternalhit|Facebot|Twitterbot|Slackbot|Slack-ImgProxy|Discordbot|WhatsApp|"
    r"TelegramBot|LinkedInBot|Pinterest|redditbot|SkypeUriPreview|Applebot|Googlebot|"
    r"bingbot|DuckDuckBot|YandexBot|W3C_Validator|vkShare",
    re.IGNORECASE,
)

AUDIO_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")


def iter_file_range(file_obj, length, chunk_size=64 * 1024):
    """Yield exactly ``length`` bytes and always close the storage handle."""
    remaining = length
    try:
        while remaining > 0:
            chunk = file_obj.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        file_obj.close()


def iter_streaming_body(streaming_body, chunk_size=64 * 1024):
    """Stream an S3/R2 response body without buffering the complete object."""
    try:
        while True:
            chunk = streaming_body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        streaming_body.close()


def open_s3_audio_stream(audio_file, start=None, end=None):
    """Open an R2/S3 object directly, forwarding an optional byte range.

    Opening an ``S3File`` through django-storages downloads the complete object
    into a spooled temporary file first. Using the configured client's
    ``get_object`` API keeps the response body streaming from object storage.
    """
    storage = audio_file.storage
    if not isinstance(storage, S3Storage):
        return None

    parameters = {
        "Bucket": storage.bucket_name,
        "Key": audio_file.name,
    }
    if start is not None and end is not None:
        parameters["Range"] = f"bytes={start}-{end}"
    return storage.connection.meta.client.get_object(**parameters)


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def is_bot_request(request):
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return bool(BOT_USER_AGENT_PATTERN.search(user_agent))


# Chapters/audios are managed as a full per-story list in the admin panel
# (not paginated UI), so this uses a high page_size rather than the global
# default of 20 — otherwise a story with more items than that would silently
# have the rest hidden.
class AdminStoryItemPagination(PageNumberPagination):
    page_size = 10000


class CataloguePagination(PageNumberPagination):
    page_size = 12


class LibraryShelfPagination(PageNumberPagination):
    page_size = 4


class AuthorPagination(PageNumberPagination):
    page_size = 24


class SearchAuthorPagination(PageNumberPagination):
    page_size = 12
    page_query_param = "author_page"


class SearchChapterPagination(PageNumberPagination):
    page_size = 12
    page_query_param = "chapter_page"


class BlogPagination(PageNumberPagination):
    page_size = 12


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


MAX_TRANSCRIPT_UPLOAD_BYTES = 5 * 1024 * 1024


def flatten_drf_errors(detail) -> str:
    """Collapse a DRF error structure (dict / list / str) into one human string."""
    if isinstance(detail, dict):
        return "; ".join(flatten_drf_errors(value) for value in detail.values())
    if isinstance(detail, (list, tuple)):
        return "; ".join(flatten_drf_errors(item) for item in detail)
    return str(detail)


def derive_transcript_state(audio, cue_count: int) -> str:
    if not rich_text_has_content(audio.transcript):
        return "empty"
    return "synchronized" if cue_count > 0 else "unsynchronized"


def audio_duration_ms(audio):
    return round(audio.duration_seconds * 1000) if audio.duration_seconds is not None else None


class AuthorViewSet(ReadOnlyModelViewSet):
    """Public author directory; detail responses only include visible stories."""

    serializer_class = AuthorSerializer
    pagination_class = AuthorPagination

    def get_queryset(self):
        return (
            Author.objects.annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .order_by("name", "id")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AuthorDetailSerializer
        return AuthorSerializer


class TagViewSet(ReadOnlyModelViewSet):
    """Public tag directory backing /tag/<slug> SEO landing pages. Unpaginated
    like Genre/Category (a small, admin-curated set), unlike AuthorViewSet."""

    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        return (
            Tag.objects.annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            # Only tags with at least one published story become a live URL —
            # keeps unused/unpublished tags from ever 404ing or, worse,
            # rendering an empty page for search engines to index.
            .filter(published_stories_count__gt=0)
            .order_by("name")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return TagDetailSerializer
        return TagSerializer


class ThemeViewSet(ReadOnlyModelViewSet):
    """Public theme directory backing /theme/<slug> SEO landing pages —
    same shape as TagViewSet, independent curation (reading experience
    rather than search-phrase keywords)."""

    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        return (
            Theme.objects.annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .filter(published_stories_count__gt=0)
            .order_by("name")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ThemeDetailSerializer
        return ThemeSerializer


class GenreViewSet(ReadOnlyModelViewSet):
    """Public genre directory — list behavior unchanged from the old
    GenreListAPIView it replaces, plus a slug-keyed retrieve backing the new
    /genre/<slug> SEO landing pages. Same shape as TagViewSet."""

    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        return (
            Genre.objects.annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .filter(published_stories_count__gt=0)
            .order_by("name")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return GenreDetailSerializer
        return GenreSerializer


class CategoryViewSet(ReadOnlyModelViewSet):
    """Public category directory — replaces the old CategoryListAPIView the
    same way GenreViewSet replaces GenreListAPIView. Same shape as
    TagViewSet; unrelated to the admin/categories CategoryAdminViewSet."""

    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        return (
            Category.objects.annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .filter(published_stories_count__gt=0)
            .order_by("name")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CategoryDetailSerializer
        return CategorySerializer


class StoryViewSet(ReadOnlyModelViewSet):
    # Keep an ungated base queryset for DRF's model introspection. The public
    # visibility predicate contains the current time and must be constructed
    # per request so scheduled stories become visible without a process restart.
    queryset = Story.objects.all()
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend]
    filterset_class = StoryFilter
    pagination_class = CataloguePagination

    def get_queryset(self):
        if self.action == "read_along":
            # Avoid the much heavier story-detail prefetches. Only the
            # requested audio query carries transcript HTML; navigation
            # selects slugs and ordering fields only.
            return (
                Story.objects.published()
                .select_related("author", "story_type")
                .only(
                    "id",
                    "title",
                    "slug",
                    "language",
                    "cover_image",
                    "cover_image_file",
                    "author_id",
                    "author__id",
                    "author__name",
                    "story_type_id",
                    "story_type__name",
                )
            )
        queryset = (
            Story.objects.published()
            .select_related("author")
            .prefetch_related(
                "genres",
                # Annotate the timed-cue flag on the prefetch so
                # AudioSerializer.transcript_synchronized doesn't N+1 on story detail.
                Prefetch(
                    "audios",
                    queryset=Audio.objects.annotate(
                        _has_timed_cues=Exists(
                            AudioTranscriptCue.objects.filter(audio=OuterRef("pk"))
                        )
                    ),
                ),
                "videos",
            )
            .order_by("-id")
        )
        # Only the "list" action (the public browse/search listing) collapses
        # each translation_group down to one edition — retrieve and the other
        # detail actions (chapter, favorite, reviews, etc.) must still resolve
        # any specific translation directly by its own slug, e.g. for the
        # Translations panel's "switch language" links to keep working.
        if self.action == "list":
            queryset = with_preferred_translation_only(
                queryset,
                preferred_language=self.request.query_params.get("language"),
            )
        return queryset

    def get_permissions(self):
        if self.action in {"reviews", "my_review"} and self.request.method in {
            "POST",
            "PATCH",
            "DELETE",
            "GET",
        }:
            if self.action == "reviews" and self.request.method == "GET":
                return []
            return [IsAuthenticated()]
        if self.action == "because_finished":
            return [IsAuthenticated()]
        return super().get_permissions()

    def _update_story_rating(self, story):
        average = story.reviews.aggregate(avg=Avg("rating")).get("avg") or 0
        story.rating = round(float(average), 1) if average else 0.0
        story.save(update_fields=["rating"])

    def _register_view(self, request, story):
        """Counts one view per IP per story per VIEW_DEDUPE_WINDOW, so refreshing the
        page or re-fetching for chapter navigation doesn't inflate the count. Bot/crawler
        requests (including the social-meta edge function's own server-side fetches) are
        excluded entirely."""
        if is_bot_request(request):
            return

        ip_address = get_client_ip(request)
        already_counted = False
        if ip_address:
            already_counted = StoryView.objects.filter(
                story=story,
                ip_address=ip_address,
                created_at__gte=timezone.now() - VIEW_DEDUPE_WINDOW,
            ).exists()

        if already_counted:
            return

        StoryView.objects.create(
            story=story,
            user=request.user if request.user.is_authenticated else None,
            ip_address=ip_address,
        )
        Story.objects.filter(pk=story.pk).update(views=F("views") + 1)
        story.views += 1

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        self._register_view(request, instance)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return StoryDetailSerializer
        return StoryListSerializer

    def _favorite_payload(self, story, user):
        is_favorite = False
        if user and user.is_authenticated:
            is_favorite = Favorite.objects.filter(story=story, user=user).exists()
        return {
            "is_favorite": is_favorite,
            "favorites_count": story.favorites.count(),
        }

    @action(detail=True, methods=["get"], url_path=r"chapters/(?P<chapter_slug>[^/.]+)")
    def chapter(self, request, slug=None, chapter_slug=None):
        story = self.get_object()
        type = request.query_params.get("type")

        if type == "text":
            try:
                chapter = story.chapters.get(slug=chapter_slug)
            except Chapter.DoesNotExist:
                return Response({"detail": "Chapter not found"}, status=404)

            serializer = ChapterSerializer(chapter)
            return Response(serializer.data)
        elif type == "audio":
            try:
                audio = story.audios.get(slug=chapter_slug)
            except Audio.DoesNotExist:
                return Response({"detail": "Audio not found"}, status=404)

            serializer = AudioSerializer(audio)
            return Response(serializer.data)
        return Response(
            {"detail": "Invalid chapter type. Use type=text or type=audio."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["get"], url_path=r"read-along/(?P<audio_slug>[^/.]+)")
    def read_along(self, request, slug=None, audio_slug=None):
        story = self.get_object()
        audio = (
            Audio.objects.filter(story_id=story.id, slug=audio_slug)
            .only(
                "id",
                "story_id",
                "title",
                "slug",
                "order",
                "audio_file",
                "transcript",
                "duration_seconds",
                "file_size_bytes",
                "read_along_offset_ms",
            )
            .prefetch_related(
                Prefetch(
                    "transcript_cues",
                    queryset=AudioTranscriptCue.objects.only(
                        "id", "audio_id", "order", "start_ms", "end_ms", "text"
                    ).order_by("order"),
                )
            )
            .first()
        )
        if audio is None:
            return Response(
                {"detail": "Audio track not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        candidate_tracks = list(
            Audio.objects.filter(story_id=story.id)
            .exclude(audio_file="")
            .only("id", "slug", "order", "transcript")
            .order_by("order", "id")
        )
        # Rich-text editors can save visually blank markup such as
        # ``<p><br></p>``. Database string checks cannot distinguish that from
        # readable text, so use the same content predicate as the availability
        # serializers before exposing previous/next navigation.
        compatible_tracks = [
            track for track in candidate_tracks if rich_text_has_content(track.transcript)
        ]
        compatible_slugs = [track.slug for track in compatible_tracks]
        if audio.slug in compatible_slugs:
            current_index = compatible_slugs.index(audio.slug)
            previous_slug = compatible_slugs[current_index - 1] if current_index > 0 else None
            next_slug = (
                compatible_slugs[current_index + 1]
                if current_index + 1 < len(compatible_slugs)
                else None
            )
        else:
            previous_slug = None
            next_slug = None

        serializer = ReadAlongSerializer(
            audio,
            context={
                "request": request,
                "story": story,
                "previous_audio_slug": previous_slug,
                "next_audio_slug": next_slug,
            },
        )
        return Response(serializer.data)

    @action(detail=True, methods=["get", "post"], url_path="reviews")
    def reviews(self, request, slug=None):
        story = self.get_object()

        if request.method == "GET":
            queryset = story.reviews.select_related("user").all()
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = ReviewSerializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            serializer = ReviewSerializer(queryset, many=True)
            return Response(serializer.data)

        if not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication credentials were not provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if Review.objects.filter(story=story, user=request.user).exists():
            return Response(
                {"detail": "You have already reviewed this story."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ReviewWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review = serializer.save(story=story, user=request.user)
        self._update_story_rating(story)
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "patch", "delete"], url_path="reviews/me")
    def my_review(self, request, slug=None):
        if not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication credentials were not provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        story = self.get_object()
        review = Review.objects.filter(story=story, user=request.user).first()

        if request.method == "GET":
            if not review:
                return Response({"detail": "Review not found."}, status=404)
            return Response(ReviewSerializer(review).data)

        if not review:
            return Response({"detail": "Review not found."}, status=404)

        if request.method == "PATCH":
            serializer = ReviewWriteSerializer(review, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            self._update_story_rating(story)
            return Response(ReviewSerializer(review).data)

        review.delete()
        self._update_story_rating(story)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post", "delete"], url_path="favorite")
    def favorite(self, request, slug=None):
        if not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication credentials were not provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        story = self.get_object()

        if request.method == "POST":
            Favorite.objects.get_or_create(story=story, user=request.user)
            return Response(self._favorite_payload(story, request.user), status=status.HTTP_200_OK)

        Favorite.objects.filter(story=story, user=request.user).delete()
        return Response(self._favorite_payload(story, request.user), status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="because-finished")
    def because_finished(self, request, slug=None):
        """Personalized "Because you finished X" rail, seeded by this
        story. Distinct from similar_stories (generic, no personalization)
        — see apps.users.recommendations.recommend_because_finished."""
        story = self.get_object()
        stories = recommend_because_finished(request.user, story)
        serializer = StoryListSerializer(stories, many=True, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="pdf-stream")
    def pdf_stream(self, request, slug=None):
        story = self.get_object()
        if not story.pdf_file:
            return Response({"detail": "PDF file not available."}, status=status.HTTP_404_NOT_FOUND)
        file_obj = story.pdf_file.open("rb")
        return FileResponse(file_obj, content_type="application/pdf")

    @action(detail=True, methods=["get"], url_path="epub-stream")
    def epub_stream(self, request, slug=None):
        story = self.get_object()
        if not story.epub_file:
            return Response({"detail": "EPUB file not available."}, status=status.HTTP_404_NOT_FOUND)
        file_obj = story.epub_file.open("rb")
        return FileResponse(file_obj, content_type="application/epub+zip")

    @action(detail=True, methods=["get"], url_path=r"audios/(?P<audio_slug>[^/.]+)/stream")
    def audio_stream(self, request, slug=None, audio_slug=None):
        """Serve audio with the byte-range semantics required by Safari."""
        story = self.get_object()
        audio = story.audios.filter(slug=audio_slug).first()
        if not audio or not audio.audio_file:
            return Response({"detail": "Audio file not available."}, status=status.HTTP_404_NOT_FOUND)

        file_size = audio.audio_file.size
        file_name = os.path.basename(audio.audio_file.name)
        safe_file_name = file_name.replace('"', "")
        content_type = mimetypes.guess_type(file_name)[0] or "audio/mpeg"
        range_header = request.headers.get("Range")

        if range_header:
            match = AUDIO_RANGE_PATTERN.fullmatch(range_header.strip())
            if not match:
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{file_size}"
                return response

            start_text, end_text = match.groups()
            if not start_text:
                suffix_length = int(end_text or 0)
                if suffix_length <= 0:
                    response = HttpResponse(status=416)
                    response["Content-Range"] = f"bytes */{file_size}"
                    return response
                start = max(0, file_size - suffix_length)
                end = file_size - 1
            else:
                start = int(start_text)
                end = min(int(end_text), file_size - 1) if end_text else file_size - 1

            if start >= file_size or start > end:
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{file_size}"
                return response

            length = end - start + 1
            remote_object = open_s3_audio_stream(audio.audio_file, start, end)
            if remote_object is not None:
                response = StreamingHttpResponse(
                    iter_streaming_body(remote_object["Body"]),
                    status=206,
                    content_type=remote_object.get("ContentType") or content_type,
                )
            else:
                file_obj = audio.audio_file.open("rb")
                file_obj.seek(start)
                response = StreamingHttpResponse(
                    iter_file_range(file_obj, length),
                    status=206,
                    content_type=content_type,
                )
            response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            response["Content-Length"] = str(length)
        else:
            remote_object = open_s3_audio_stream(audio.audio_file)
            if remote_object is not None:
                response = StreamingHttpResponse(
                    iter_streaming_body(remote_object["Body"]),
                    content_type=remote_object.get("ContentType") or content_type,
                )
            else:
                file_obj = audio.audio_file.open("rb")
                response = FileResponse(file_obj, content_type=content_type)
            response["Content-Length"] = str(file_size)

        response["Accept-Ranges"] = "bytes"
        response["Content-Disposition"] = f'inline; filename="{safe_file_name}"'
        response["Cache-Control"] = "public, max-age=3600"
        return response


class BlogViewSet(ReadOnlyModelViewSet):
    queryset = Blog.objects.all()
    lookup_field = "slug"
    serializer_class = BlogSerializer
    pagination_class = BlogPagination
    filter_backends = [SearchFilter]
    search_fields = ["title", "excerpt"]

    def get_queryset(self):
        queryset = Blog.objects.published().prefetch_related(
            Prefetch(
                "linked_stories",
                queryset=Story.objects.published().select_related("author", "story_type"),
                to_attr="published_linked_stories",
            ),
            Prefetch(
                "linked_blogs",
                queryset=Blog.objects.published(),
                to_attr="published_linked_blogs",
            ),
        ).order_by("-created_at")
        if self.request.query_params.get("sort") == "oldest":
            queryset = queryset.order_by("created_at")
        linked = self.request.query_params.get("linked_to_story")
        if linked == "true":
            queryset = queryset.filter(linked_stories__isnull=False).distinct()
        elif linked == "false":
            queryset = queryset.filter(linked_stories__isnull=True)
        linked_story_slug = self.request.query_params.get("linked_story")
        if linked_story_slug:
            queryset = queryset.filter(linked_stories__slug=linked_story_slug).distinct()
        return queryset


class SubmissionViewSet(ModelViewSet):
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = Submission.objects.select_related("user").prefetch_related("genres")
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == "list":
            return SubmissionListSerializer
        return SubmissionSerializer

    def partial_update(self, request, *args, **kwargs):
        submission = self.get_object()
        if request.user.is_staff:
            return super().partial_update(request, *args, **kwargs)
        if submission.status != "requires_edit":
            return Response(
                {"detail": "Only submissions marked Requires Edit can be edited."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = self.get_serializer(submission, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(
            status="pending",
            reviewer_notes=None,
            reviewed_by=None,
            reviewed_at=None,
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        submission = self.get_object()
        if submission.status == "approved" and not request.user.is_staff:
            return Response(
                {"detail": "Approved submissions cannot be deleted."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)


_VALID_INPUT_FIELD_SETS = (
    frozenset({"title", "author"}),
    frozenset({"title", "author", "content"}),
)


class StoryAdminViewSet(ModelViewSet):
    queryset = Story.objects.select_related("author", "submitted_by", "submission").all().order_by("-id")
    serializer_class = StoryAdminSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "about"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params

        is_published = params.get("is_published")
        if is_published in {"true", "false"}:
            queryset = queryset.filter(is_published=is_published == "true")

        is_completed = params.get("is_completed")
        if is_completed in {"true", "false"}:
            queryset = queryset.filter(is_completed=is_completed == "true")

        has_summary = params.get("has_summary")
        if has_summary in {"true", "false"}:
            no_summary = Q(summary__isnull=True) | Q(summary__exact="")
            queryset = queryset.filter(no_summary) if has_summary == "false" else queryset.exclude(no_summary)

        has_retrospective = params.get("has_retrospective")
        if has_retrospective in {"true", "false"}:
            no_retrospective = Q(retrospective__isnull=True) | Q(retrospective__exact="")
            queryset = (
                queryset.filter(no_retrospective)
                if has_retrospective == "false"
                else queryset.exclude(no_retrospective)
            )

        return queryset

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """CSV export for the Story Report page — Story rows (respecting
        whatever report filters/search are currently applied, same query
        params as the list endpoint, unpaginated), optionally with every
        not-yet-added StoryQueue row appended (build_story_export_csv's job,
        not filtered by these report params — queue items aren't Story
        rows). include_stories/include_queue (?include_stories=false etc.,
        both default true) let the admin pick either source, both, or
        neither. Same column schema queue_import.py expects, so the file can
        be re-imported elsewhere unchanged."""
        include_stories = request.query_params.get("include_stories", "true").lower() != "false"
        include_queue = request.query_params.get("include_queue", "true").lower() != "false"
        queryset = self.filter_queryset(self.get_queryset())
        csv_text = build_story_export_csv(
            queryset, request, include_stories=include_stories, include_queue=include_queue
        )
        response = HttpResponse(csv_text, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="stories-export.csv"'
        return response

    @action(detail=False, methods=["post"], url_path="bulk-taxonomy-preview")
    def bulk_taxonomy_preview(self, request):
        """Parses an uploaded CSV/Excel file and previews the tags/themes/
        genres/categories changes it would make to already-published Story
        rows (matched by title, disambiguated by author_name) — writes
        nothing to the DB. See taxonomy_bulk_update.build_taxonomy_preview."""
        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response({"detail": "Upload a CSV or Excel (.xlsx) file."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(build_taxonomy_preview(uploaded_file))
        except ImportFileError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="bulk-taxonomy-confirm")
    def bulk_taxonomy_confirm(self, request):
        """Applies the admin-reviewed subset of a bulk-taxonomy-preview's
        matched rows. Re-resolves each row's story match defensively before
        writing. See taxonomy_bulk_update.confirm_taxonomy_update."""
        records = request.data.get("records")
        if not isinstance(records, list) or not records:
            return Response({"detail": "No records to update."}, status=status.HTTP_400_BAD_REQUEST)
        if len(records) > MAX_IMPORT_ROWS:
            return Response(
                {"detail": f"Too many records — max {MAX_IMPORT_ROWS} per update."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = confirm_taxonomy_update(records)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="link-translation")
    def link_translation(self, request, pk=None):
        story = self.get_object()
        target_id = request.data.get("target_story_id")
        if not target_id:
            return Response({"detail": "target_story_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            target = Story.objects.get(pk=target_id)
        except Story.DoesNotExist:
            return Response({"detail": "Target story not found."}, status=status.HTTP_404_NOT_FOUND)
        if target.pk == story.pk:
            return Response(
                {"detail": "A story cannot be linked to itself."}, status=status.HTTP_400_BAD_REQUEST
            )
        story.translation_group = target.translation_group
        story.save(update_fields=["translation_group"])
        return Response(self.get_serializer(story).data)

    @action(detail=True, methods=["post"], url_path="unlink-translation")
    def unlink_translation(self, request, pk=None):
        story = self.get_object()
        story.translation_group = uuid.uuid4()
        story.save(update_fields=["translation_group"])
        return Response(self.get_serializer(story).data)

    @action(detail=True, methods=["post"], url_path="import-epub")
    def import_epub(self, request, pk=None):
        story = self.get_object()

        # If the story already has an epub_file, import from that (no
        # re-upload needed). Otherwise the request may carry a new file
        # directly — validated with the same validators declared on
        # Story.epub_file itself (extension + size cap), then saved onto
        # the story so it's the one source of truth for future imports.
        uploaded_file = request.FILES.get("epub_file")
        if uploaded_file:
            epub_field = Story._meta.get_field("epub_file")
            try:
                for validator in epub_field.validators:
                    validator(uploaded_file)
            except DjangoValidationError as exc:
                return Response({"detail": "; ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
            story.epub_file = uploaded_file
            story.save(update_fields=["epub_file"])
        elif not story.epub_file:
            return Response(
                {"detail": "Upload an EPUB file to import chapters from."}, status=status.HTTP_400_BAD_REQUEST
            )

        job = EpubImportJob.objects.create(story=story, status=EpubImportJob.STATUS_PENDING)
        # Submitted only after this view's own transaction commits — the
        # worker thread opens its own DB connection and must never race the
        # still-open request transaction (ATOMIC_REQUESTS=True). See
        # epub_import_jobs.py's module docstring.
        transaction.on_commit(lambda: epub_import_executor.submit(run_epub_import, job.id))
        return Response(EpubImportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"], url_path=r"import-epub/(?P<job_id>\d+)")
    def import_epub_status(self, request, pk=None, job_id=None):
        try:
            job = EpubImportJob.objects.get(pk=job_id, story_id=pk)
        except EpubImportJob.DoesNotExist:
            return Response({"detail": "Import job not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(EpubImportJobSerializer(job).data)

    @action(detail=True, methods=["post"], url_path="generate-summary")
    def generate_summary(self, request, pk=None):
        return self._trigger_generation(request, "summary")

    @action(detail=True, methods=["post"], url_path="generate-retrospective")
    def generate_retrospective(self, request, pk=None):
        return self._trigger_generation(request, "retrospective")

    def _trigger_generation(self, request, action):
        story = self.get_object()
        input_fields = request.data.get("input_fields", ["title", "author", "content"])
        if (
            not isinstance(input_fields, list)
            or len(set(input_fields)) != len(input_fields)
            or frozenset(input_fields) not in _VALID_INPUT_FIELD_SETS
        ):
            return Response(
                {"detail": 'input_fields must be exactly ["title", "author"] or ["title", "author", "content"].'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if "content" in input_fields and not story.chapters.exists():
            return Response(
                {"detail": "This story has no chapters to generate from. Add chapters first, or omit \"content\"."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        Story.objects.filter(pk=story.pk).update(**{f"{action}_status": Story.GEN_STATUS_PENDING})
        story.refresh_from_db()
        transaction.on_commit(
            lambda: ai_generation_executor.submit(run_generate_field, story.id, action, input_fields)
        )
        return Response(self.get_serializer(story).data, status=status.HTTP_202_ACCEPTED)


class StoryQueueViewSet(ModelViewSet):
    """A backlog of title/author ideas an admin plans to eventually publish
    as real stories — see StoryQueue's docstring. list/create/destroy are
    the plain CRUD for managing the backlog itself; the "add" action is the
    one meaningful custom behavior: turning one entry into a real, draft
    Story."""

    queryset = StoryQueue.objects.select_related("added_story").prefetch_related("genres", "categories", "tags", "themes").all()
    serializer_class = StoryQueueSerializer
    permission_classes = [IsSuperUser]

    def get_queryset(self):
        queryset = super().get_queryset()
        is_added = self.request.query_params.get("is_added")
        if is_added in {"true", "false"}:
            queryset = queryset.filter(is_added=is_added == "true")
        return queryset

    @action(detail=False, methods=["get"], url_path="check-title")
    def check_title(self, request):
        """Live title suggestions/duplicate check as the admin types into the
        "Add to Queue" form. Prefix-matches case-insensitively against both
        Story (any status) and StoryQueue entries not yet added — an
        already-added queue entry is a duplicate of the Story it produced,
        which the Story-side check already catches. Prefix (not exact)
        matching so results appear progressively while typing, same as the
        genre/category suggestion lists elsewhere in this form.

        When editing an existing queue item, the Edit form passes its own id
        as exclude_queue_id so the item doesn't flag itself (and its own
        added_story, if it has one) as a duplicate of itself."""
        title = request.query_params.get("title", "").strip()
        if not title:
            return Response({"story_matches": [], "queue_matches": []})

        story_queryset = Story.objects.filter(title__istartswith=title)
        queue_queryset = StoryQueue.objects.filter(title__istartswith=title, is_added=False)

        exclude_queue_id = request.query_params.get("exclude_queue_id")
        if exclude_queue_id:
            queue_queryset = queue_queryset.exclude(id=exclude_queue_id)
            excluded_item = (
                StoryQueue.objects.filter(id=exclude_queue_id).only("added_story_id").first()
            )
            if excluded_item and excluded_item.added_story_id:
                story_queryset = story_queryset.exclude(id=excluded_item.added_story_id)

        story_matches = [
            {"id": story.id, "title": story.title, "slug": story.slug, "is_published": story.is_published}
            for story in story_queryset.only("id", "title", "slug", "is_published")[:8]
        ]
        queue_matches = [
            {"id": item.id, "title": item.title, "author_name": item.author_name}
            for item in queue_queryset.only("id", "title", "author_name")[:8]
        ]
        return Response({"story_matches": story_matches, "queue_matches": queue_matches})

    @action(detail=False, methods=["post"], url_path="fetch-books")
    def fetch_books(self, request):
        """Kicks off the "Fetch Book Data" AI action: asks Claude for
        `count` public-domain books not already in Story/StoryQueue, then
        creates new StoryQueue rows from whatever survives dedup. See
        book_fetch.py (prompt/API call) and book_fetch_jobs.py (DB-touching
        worker) — same async-job shape as import_epub/EpubImportJob."""
        try:
            count = int(request.data.get("count", DEFAULT_BOOK_FETCH_COUNT))
        except (TypeError, ValueError):
            return Response({"detail": "count must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= count <= MAX_BOOK_FETCH_COUNT):
            return Response(
                {"detail": f"count must be between 1 and {MAX_BOOK_FETCH_COUNT}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job = BookFetchJob.objects.create(requested_count=count, status=BookFetchJob.STATUS_PENDING)
        transaction.on_commit(lambda: book_fetch_executor.submit(run_book_fetch, job.id))
        return Response(BookFetchJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["get"], url_path=r"fetch-books/(?P<job_id>\d+)")
    def fetch_books_status(self, request, job_id=None):
        try:
            job = BookFetchJob.objects.get(pk=job_id)
        except BookFetchJob.DoesNotExist:
            return Response({"detail": "Fetch job not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(BookFetchJobSerializer(job).data)

    @action(detail=False, methods=["post"], url_path="import-preview")
    def import_preview(self, request):
        """Parses+dedupes an uploaded CSV/Excel file and returns what would
        be added/skipped/rejected — writes nothing to the DB. See
        queue_import.build_preview."""
        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response({"detail": "Upload a CSV or Excel (.xlsx) file."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(build_preview(uploaded_file))
        except ImportFileError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="import-confirm")
    def import_confirm(self, request):
        """Creates StoryQueue rows for the admin-reviewed subset of an
        import-preview's "to_add" list. Re-dedupes defensively against the
        current DB state before writing. See queue_import.confirm_import."""
        records = request.data.get("records")
        if not isinstance(records, list) or not records:
            return Response({"detail": "No records to import."}, status=status.HTTP_400_BAD_REQUEST)
        if len(records) > MAX_IMPORT_ROWS:
            return Response(
                {"detail": f"Too many records — max {MAX_IMPORT_ROWS} per import."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        created_count, skipped_count = confirm_import(records)
        return Response(
            {"created_count": created_count, "skipped_count": skipped_count}, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="add", url_name="add")
    def add_to_stories(self, request, pk=None):
        queue_item = self.get_object()
        if queue_item.is_added:
            return Response(
                {"detail": "This queue item has already been added."}, status=status.HTTP_400_BAD_REQUEST
            )

        story_data = {
            "title": queue_item.title,
            "is_published": False,
            "about": queue_item.about or "",
            "genres": [genre.id for genre in queue_item.genres.all()],
            "categories": [category.id for category in queue_item.categories.all()],
            "tags": [tag.id for tag in queue_item.tags.all()],
            "themes": [theme.id for theme in queue_item.themes.all()],
            "original_published_year": queue_item.original_published_year,
            "original_published_month": queue_item.original_published_month,
            "original_published_day": queue_item.original_published_day,
        }
        if queue_item.author_name:
            author, _ = Author.objects.get_or_create(name=queue_item.author_name)
            story_data["author"] = author.id
        # Story.story_type is required — fall back to the same default a
        # story would get if created with no explicit choice, since a queue
        # item's story_type is optional but a real Story's isn't.
        story_data["story_type"] = queue_item.story_type_id or default_story_type_id()
        if queue_item.country:
            story_data["country"] = queue_item.country
        if queue_item.language:
            story_data["language"] = queue_item.language
        # cover_image is a plain URLField on Story (not an upload), so the
        # public-domain reference link copies straight across. epub_link/
        # pdf_link are intentionally NOT copied — see StoryQueue's docstring.
        if queue_item.cover_image_link:
            story_data["cover_image"] = queue_item.cover_image_link

        story_serializer = StoryAdminSerializer(data=story_data, context={"request": request})
        story_serializer.is_valid(raise_exception=True)
        story = story_serializer.save()

        # A queue entry has no chapter structure of its own — a non-blank
        # content value is the whole story text (short stories only), so it
        # becomes the new Story's single chapter rather than living on
        # Story itself. Mirrors _publish_submission's identical pattern.
        if queue_item.content.strip():
            Chapter.objects.create(
                story=story,
                title="Chapter 1",
                slug="chapter-1",
                content=queue_item.content,
                order=1,
            )

        queue_item.is_added = True
        queue_item.added_story = story
        queue_item.save(update_fields=["is_added", "added_story"])

        return Response(StoryQueueSerializer(queue_item).data, status=status.HTTP_201_CREATED)


class BlogAdminViewSet(ModelViewSet):
    queryset = Blog.objects.prefetch_related("linked_stories", "linked_blogs").all().order_by("-created_at")
    serializer_class = BlogAdminSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsSuperUser]
    pagination_class = BlogPagination
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "excerpt"]

    @action(detail=True, methods=["post"], url_path="generate-excerpt")
    def generate_excerpt(self, request, pk=None):
        blog = self.get_object()
        Blog.objects.filter(pk=blog.pk).update(excerpt_status=Blog.GEN_STATUS_PENDING)
        blog.refresh_from_db()
        transaction.on_commit(lambda: ai_generation_executor.submit(run_generate_blog_excerpt, blog.id))
        return Response(self.get_serializer(blog).data, status=status.HTTP_202_ACCEPTED)


class ChapterAdminViewSet(ModelViewSet):
    queryset = Chapter.objects.select_related("story").all().order_by("story_id", "order")
    serializer_class = ChapterAdminSerializer
    pagination_class = AdminStoryItemPagination
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "story__title"]

    def get_queryset(self):
        queryset = super().get_queryset()
        story_id = self.request.query_params.get("story")
        if story_id:
            queryset = queryset.filter(story_id=story_id).order_by("order", "id")
        return queryset

    def perform_destroy(self, instance):
        story = instance.story
        deleted_order = instance.order
        instance.delete()

        # Close the gap left by the deleted chapter so order stays
        # contiguous from 1 — chapters before it are untouched, chapters
        # after it each shift down by one. Updated ascending so every write
        # lands on the slot the previous iteration just vacated, never
        # colliding with unique_together("story", "order").
        later_chapters = Chapter.objects.filter(story=story, order__gt=deleted_order).order_by("order")
        for chapter in later_chapters:
            chapter.order -= 1
            chapter.save(update_fields=["order"])


class AudioAdminViewSet(ModelViewSet):
    queryset = Audio.objects.select_related("story").all().order_by("story_id", "order")
    serializer_class = AudioAdminSerializer
    pagination_class = AdminStoryItemPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "story__title"]

    def get_queryset(self):
        queryset = super().get_queryset().annotate(
            _cue_count=Count("transcript_cues"),
            _has_timed_cues=Exists(
                AudioTranscriptCue.objects.filter(audio=OuterRef("pk"))
            ),
        )
        story_id = self.request.query_params.get("story")
        if story_id:
            queryset = queryset.filter(story_id=story_id).order_by("order", "id")
        return queryset

    def _validated_cues(self, audio, cue_dicts):
        """Run cue dicts through the shared serializer (per-cue + sequence rules).
        Returns validated data or raises a 400-ready message string."""
        serializer = AudioTranscriptCueSerializer(
            data=cue_dicts,
            many=True,
            context={"audio_duration_ms": audio_duration_ms(audio)},
        )
        if not serializer.is_valid():
            raise TranscriptParseError(flatten_drf_errors(serializer.errors))
        return serializer.validated_data

    def _replace_cues(self, audio, validated_cues):
        audio.transcript_cues.all().delete()
        if validated_cues:
            AudioTranscriptCue.objects.bulk_create(
                [AudioTranscriptCue(audio=audio, **dict(row)) for row in validated_cues]
            )

    def _transcript_payload(self, audio, cue_count):
        return {
            "transcript_state": derive_transcript_state(audio, cue_count),
            "cue_count": cue_count,
            "transcript": audio.transcript,
        }

    @action(detail=True, methods=["post"], url_path="import-transcript")
    def import_transcript(self, request, pk=None):
        audio = self.get_object()

        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size and upload.size > MAX_TRANSCRIPT_UPLOAD_BYTES:
                return Response(
                    {"detail": "Transcript file is too large (5 MB max)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            fmt = format_from_filename(upload.name)
            if fmt is None:
                return Response(
                    {"detail": "Upload a .vtt, .srt, or .txt file."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            content = upload.read().decode("utf-8", errors="replace")
        else:
            content = request.data.get("content")
            fmt = request.data.get("format")
            if not content or fmt not in SUPPORTED_FORMATS:
                return Response(
                    {"detail": "Provide a transcript file, or content plus a valid format."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            result = parse_transcript(content, fmt)
            cue_dicts = [
                {"order": c.order, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                for c in result.cues
            ]
            validated = self._validated_cues(audio, cue_dicts)
        except TranscriptParseError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Every check has passed before anything is written, so a failure above
        # leaves the previous cues + transcript fully intact.
        seed_transcript = (not result.is_timed) or (not rich_text_has_content(audio.transcript))
        with transaction.atomic():
            self._replace_cues(audio, validated)
            if seed_transcript:
                audio.transcript = result.transcript_html
                audio.save(update_fields=["transcript"])

        return Response(self._transcript_payload(audio, len(validated)))

    @action(detail=True, methods=["get", "put"], url_path="transcript-cues")
    def transcript_cues(self, request, pk=None):
        audio = self.get_object()

        if request.method == "GET":
            cues = list(
                audio.transcript_cues.values("id", "order", "start_ms", "end_ms", "text")
            )
            return Response(
                {
                    "cues": cues,
                    "cue_count": len(cues),
                    "transcript_state": derive_transcript_state(audio, len(cues)),
                }
            )

        try:
            validated = self._validated_cues(audio, request.data.get("cues", []))
        except TranscriptParseError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if validated and not rich_text_has_content(audio.transcript):
            return Response(
                {"detail": "Add or import transcript text before saving timed cues."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            self._replace_cues(audio, validated)

        return Response(self._transcript_payload(audio, len(validated)))

    @action(detail=True, methods=["post"], url_path="clear-transcript")
    def clear_transcript(self, request, pk=None):
        audio = self.get_object()
        with transaction.atomic():
            audio.transcript_cues.all().delete()
            audio.transcript = ""
            audio.save(update_fields=["transcript"])
        return Response({"transcript_state": "empty", "cue_count": 0, "transcript": ""})


class VideoAdminViewSet(ModelViewSet):
    queryset = Video.objects.select_related("story").all().order_by("story_id", "order")
    serializer_class = VideoAdminSerializer
    pagination_class = AdminStoryItemPagination
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "story__title"]

    def get_queryset(self):
        queryset = super().get_queryset()
        story_id = self.request.query_params.get("story")
        if story_id:
            queryset = queryset.filter(story_id=story_id).order_by("order", "id")
        return queryset

    def perform_destroy(self, instance):
        story = instance.story
        deleted_order = instance.order
        instance.delete()

        # Keep order contiguous from 1 — see ChapterAdminViewSet.perform_destroy.
        later_videos = Video.objects.filter(story=story, order__gt=deleted_order).order_by("order")
        for video in later_videos:
            video.order -= 1
            video.save(update_fields=["order"])


class SubmissionAdminViewSet(ModelViewSet):
    queryset = (
        Submission.objects.select_related("user", "reviewed_by", "published_story")
        .prefetch_related("genres")
        .order_by("-created_at")
    )
    serializer_class = SubmissionAdminSerializer
    permission_classes = [IsSuperUser]
    http_method_names = ["get", "patch", "head", "options"]
    filter_backends = [SearchFilter]
    search_fields = ["title", "user__email", "status"]

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        if status_filter and status_filter in {"pending", "requires_edit", "approved", "rejected"}:
            queryset = queryset.filter(status=status_filter)
        return queryset

    def perform_update(self, serializer):
        save_kwargs = {}
        next_status = serializer.validated_data.get("status")
        next_notes = serializer.validated_data.get("reviewer_notes")
        if next_status == "requires_edit" and not (next_notes and str(next_notes).strip()):
            raise ValidationError({"reviewer_notes": "Reviewer notes are required when requesting edits."})
        if "status" in serializer.validated_data:
            from django.utils import timezone

            save_kwargs["reviewed_by"] = self.request.user
            save_kwargs["reviewed_at"] = timezone.now()
        with transaction.atomic():
            submission = serializer.save(**save_kwargs)
            if next_status == "approved":
                story = self._upsert_story_draft_from_submission(submission)
                submission.published_story = story
                submission.save(update_fields=["published_story"])

    def _build_unique_story_slug(self, title: str, instance=None) -> str:
        base_slug = slugify(title) or "story"
        slug = base_slug
        suffix = 2
        queryset = Story.objects.all()
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def _upsert_story_draft_from_submission(self, submission: Submission) -> Story:
        story = submission.published_story
        if story is None:
            story = Story(
                title=submission.title,
                slug=self._build_unique_story_slug(submission.title),
            )

        story.title = submission.title
        story.about = submission.about
        story.story_type = submission.story_type
        story.language = submission.language
        story.submitted_by = submission.user
        story.cover_image = submission.cover_image
        if submission.cover_image_file:
            story.cover_image_file = submission.cover_image_file
        if submission.pdf_file:
            story.pdf_file = submission.pdf_file
        if submission.epub_file:
            story.epub_file = submission.epub_file
        story.original_published_year = None
        story.original_published_month = None
        story.original_published_day = None
        story.site_published_date = None
        story.is_published = False
        story.save()
        story.genres.set(submission.genres.all())

        if not story.chapters.exists():
            Chapter.objects.create(
                story=story,
                title="Chapter 1",
                slug="chapter-1",
                content=submission.content,
                order=1,
            )
        return story


class LibraryShelvesAPIView(APIView):
    """Paginates genres (the 'shelves'), not stories — each shelf carries only a small
    preview of its stories. This keeps a genre-organized library view cheap regardless
    of how large the catalog gets: only the shelves currently on screen (plus a page
    of lookahead) ever hit the database."""

    PREVIEW_SIZE = 8

    def get(self, request):
        # Collapses each translation_group to a single (English-preferred)
        # edition first, so both the per-genre counts and the preview lists
        # below only ever reflect one entry per underlying work.
        preferred_stories = with_preferred_translation_only(Story.objects.published())

        genres = (
            Genre.objects.filter(stories__in=preferred_stories)
            .annotate(
                published_stories_count=Count(
                    "stories",
                    filter=Q(stories__in=preferred_stories),
                    distinct=True,
                )
            )
            .order_by("name")
        )

        paginator = LibraryShelfPagination()
        page = paginator.paginate_queryset(genres, request)
        paginator.aggregate = {
            "total_stories": preferred_stories.count()
        }

        shelves = []
        for genre in page:
            preview_stories = (
                preferred_stories.filter(genres=genre)
                .select_related("author")
                .prefetch_related("genres", "audios", "videos")
                .order_by("-views", "-rating", "-id")[: self.PREVIEW_SIZE]
            )
            shelves.append(
                {
                    "id": genre.id,
                    "name": genre.name,
                    "stories_count": genre.published_stories_count,
                    "preview_stories": StoryListSerializer(
                        preview_stories, many=True, context={"request": request}
                    ).data,
                }
            )

        return paginator.get_paginated_response(shelves)


class AuthorAdminViewSet(ModelViewSet):
    """Full author management (list/create/update/delete) for the admin panel.

    Deletion is blocked while the author still has stories — Story.author uses
    on_delete=CASCADE, so an unguarded delete here would silently wipe out
    every one of that author's stories along with them.
    """

    queryset = Author.objects.all().order_by("name")
    serializer_class = AdminAuthorSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name", "bio"]

    def destroy(self, request, *args, **kwargs):
        author = self.get_object()
        stories_count = author.stories.count()
        if stories_count:
            return Response(
                {
                    "detail": (
                        f"Can't delete \"{author.name}\" — {stories_count} "
                        f"{'story is' if stories_count == 1 else 'stories are'} still "
                        "assigned to them. Reassign or delete those stories first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


def _unique_category_slug(name: str) -> str:
    base_slug = slugify(name) or "category"
    slug = base_slug
    index = 2
    while Category.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


class CategoryAdminViewSet(ModelViewSet):
    """Full management CRUD (list/create/update/delete) for the admin
    panel's Categories page. Delete needs no in-use guard, unlike
    StoryTypeAdminViewSet — categories/genres/tags/themes are all
    many-to-many with Story, so removing one just detaches it from
    whatever stories had it, it never blocks or cascades into deleting
    a Story the way a PROTECTed ForeignKey would."""

    queryset = Category.objects.all().order_by("name")
    serializer_class = AdminCategorySerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]

    def perform_create(self, serializer):
        name = serializer.validated_data["name"]
        serializer.save(slug=_unique_category_slug(name))


class StoryTypeViewSet(ReadOnlyModelViewSet):
    """Public, read-only — the live list of story types for any form that
    needs to offer a choice (Library's filter, the public submission form).
    No permission override needed (AllowAny). Unpaginated, unlike
    AuthorViewSet — story types are a small, curated set meant to be listed
    in full for a dropdown, not paged through."""

    queryset = StoryType.objects.all().order_by("name")
    serializer_class = StoryTypeSerializer
    pagination_class = None


class StoryTypeAdminViewSet(ModelViewSet):
    """Full story-type management (list/create/update/delete) for the admin
    panel. Deletion is blocked while the type is still referenced by any
    Story, StoryQueue entry, or Submission — story_type uses
    on_delete=PROTECT everywhere, so an unguarded delete would otherwise hit
    an unfriendly IntegrityError instead of a clear explanation."""

    queryset = StoryType.objects.all().order_by("name")
    serializer_class = AdminStoryTypeSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]

    def destroy(self, request, *args, **kwargs):
        story_type = self.get_object()
        in_use = (
            story_type.stories.exists()
            or story_type.queue_items.exists()
            or story_type.submissions.exists()
        )
        if in_use:
            return Response(
                {"detail": f"Can't delete \"{story_type.name}\" — it's still in use. Reassign those first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


def _unique_genre_slug(name: str) -> str:
    base_slug = slugify(name) or "genre"
    slug = base_slug
    index = 2
    while Genre.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


class GenreAdminViewSet(ModelViewSet):
    """Full management CRUD for the admin panel's Genres page — same shape
    as CategoryAdminViewSet."""

    queryset = Genre.objects.all().order_by("name")
    serializer_class = AdminGenreSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]

    def perform_create(self, serializer):
        name = serializer.validated_data["name"]
        serializer.save(slug=_unique_genre_slug(name))


def _unique_tag_slug(name: str) -> str:
    base_slug = slugify(name) or "tag"
    slug = base_slug
    index = 2
    while Tag.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


class TagAdminViewSet(ModelViewSet):
    """Full management CRUD for the admin panel's Tags page — replaces the
    old list+create-only AdminTagListCreateAPIView; the quick-add "create a
    tag while editing a story" flow (AdminContent.tsx/StoryQueueManager.tsx)
    still POSTs to this same /admin/tags/ endpoint unchanged."""

    queryset = Tag.objects.all().order_by("name")
    serializer_class = AdminTagSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]

    def perform_create(self, serializer):
        name = serializer.validated_data["name"]
        serializer.save(slug=_unique_tag_slug(name))


def _unique_theme_slug(name: str) -> str:
    base_slug = slugify(name) or "theme"
    slug = base_slug
    index = 2
    while Theme.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


class ThemeAdminViewSet(ModelViewSet):
    """Full management CRUD for the admin panel's Themes page — same
    relationship to AdminThemeListCreateAPIView as TagAdminViewSet has to
    AdminTagListCreateAPIView."""

    queryset = Theme.objects.all().order_by("name")
    serializer_class = AdminThemeSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]

    def perform_create(self, serializer):
        name = serializer.validated_data["name"]
        serializer.save(slug=_unique_theme_slug(name))


class HomeDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.published().select_related("author").prefetch_related("genres", "audios", "videos")
        used_ids = set()

        def take(queryset, limit):
            """Pull `limit` stories off the front of an ordered queryset, skipping any
            story already claimed by an earlier (more prominent) section on this page.
            Sections are computed in page order, top to bottom, so each one shows
            stories the reader hasn't already seen higher up the homepage."""
            results = []
            for story in queryset:
                if story.id in used_ids:
                    continue
                results.append(story)
                used_ids.add(story.id)
                if len(results) >= limit:
                    break
            return results

        featured_stories = take(base_qs.order_by("-views", "-rating", "-id"), 5)
        weekly_spotlight = take(base_qs.order_by("-rating", "-views", "-id"), 6)
        sidebar_recommended = take(base_qs.order_by("-rating", "-views", "-id"), 3)
        new_trending = take(base_qs.order_by("-views", "-site_published_date", "-id"), 5)
        recommended = take(base_qs.order_by("-rating", "-site_published_date", "-id"), 6)
        popular = take(base_qs.order_by("-views", "-rating", "-id"), 6)
        new_releases = take(base_qs.order_by("-site_published_date", "-id"), 6)
        more_to_explore = take(base_qs.order_by("-id"), 12)
        # Independent of `used_ids` — this is a distinctly-filtered set (has a
        # summary) rather than a "next best" pick from the shared pool, and
        # gating it on the other sections' leftovers could easily leave it
        # empty while summary-having stories are actually available.
        quick_reads = list(
            base_qs.exclude(Q(summary__isnull=True) | Q(summary__exact=""))
            .order_by("-site_published_date", "-id")[:8]
        )

        readers_count = (
            Story.objects.published().aggregate(total_readers=Sum("views")).get("total_readers") or 0
        )

        return Response(
            {
                "featured_stories": FeaturedStorySerializer(
                    featured_stories, many=True, context={"request": request}
                ).data,
                "weekly_spotlight": StoryListSerializer(
                    weekly_spotlight, many=True, context={"request": request}
                ).data,
                "new_trending": StoryListSerializer(
                    new_trending, many=True, context={"request": request}
                ).data,
                "more_to_explore": StoryListSerializer(
                    more_to_explore, many=True, context={"request": request}
                ).data,
                "quick_reads": StoryListSerializer(
                    quick_reads, many=True, context={"request": request}
                ).data,
                "tabs": {
                    "recommended": StoryListSerializer(
                        recommended, many=True, context={"request": request}
                    ).data,
                    "popular": StoryListSerializer(
                        popular, many=True, context={"request": request}
                    ).data,
                    "new": StoryListSerializer(
                        new_releases, many=True, context={"request": request}
                    ).data,
                },
                "sidebar": {
                    "recommended": StoryListSerializer(
                        sidebar_recommended, many=True, context={"request": request}
                    ).data,
                    "stats": {
                        "creators": Author.objects.count(),
                        "stories": Story.objects.published().count(),
                        "readers": readers_count,
                    },
                },
            }
        )


class TrendingDataAPIView(APIView):
    def get(self, request):
        base_qs = (
            Story.objects.published()
            .select_related("author")
            .prefetch_related("genres", "audios", "videos")
            .annotate(
                favorites_total=Count("favorites", distinct=True),
                reviews_total=Count("reviews", distinct=True),
            )
        )
        return Response(
            {
                "most_viewed": StoryListSerializer(
                    base_qs.order_by("-views", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "highest_rated": StoryListSerializer(
                    base_qs.order_by("-rating", "-views", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "most_favorited": StoryListSerializer(
                    base_qs.order_by("-favorites_total", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "most_discussed": StoryListSerializer(
                    base_qs.order_by("-reviews_total", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
            }
        )


class OriginalsDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.published().select_related("author").prefetch_related("genres", "audios", "videos")
        return Response(
            {
                "stories": StoryListSerializer(
                    base_qs.order_by("-id", "-rating")[:20],
                    many=True,
                    context={"request": request},
                ).data
            }
        )


class DiscoverDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.published().select_related("author").prefetch_related("genres", "audios", "videos")
        trending_qs = base_qs.annotate(
            favorites_total=Count("favorites", distinct=True),
            reviews_total=Count("reviews", distinct=True),
        )

        story_types = (
            StoryType.objects.filter(published_story_q("stories"))
            .annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .order_by("name")
        )
        language_counts = {
            item["language"]: item["stories_count"]
            for item in Story.objects.published()
            .values("language")
            .annotate(stories_count=Count("id"))
        }
        languages = [
            {
                "value": value,
                "label": label,
                "stories_count": language_counts[value],
            }
            for value, label in LANGUAGE_CHOICES
            if value in language_counts
        ]
        genres = (
            Genre.objects.filter(published_story_q("stories"))
            .annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .order_by("name")
        )
        categories = (
            Category.objects.filter(published_story_q("stories"))
            .annotate(
                published_stories_count=Count(
                    "stories",
                    filter=published_story_q("stories"),
                    distinct=True,
                )
            )
            .order_by("name")
        )
        return Response(
            {
                "genres": GenreSerializer(genres, many=True).data,
                "categories": CategorySerializer(categories, many=True).data,
                "story_types": StoryTypeSerializer(story_types, many=True).data,
                "languages": languages,
                "most_viewed": StoryListSerializer(
                    trending_qs.order_by("-views", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "highest_rated": StoryListSerializer(
                    trending_qs.order_by("-rating", "-views", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "most_favorited": StoryListSerializer(
                    trending_qs.order_by("-favorites_total", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "most_discussed": StoryListSerializer(
                    trending_qs.order_by("-reviews_total", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "new_releases": StoryListSerializer(
                    base_qs.order_by("-site_published_date", "-id")[:20],
                    many=True,
                    context={"request": request},
                ).data,
                "hidden_gems": StoryListSerializer(
                    base_qs.order_by("-rating", "views", "-id")[:20],
                    many=True,
                    context={"request": request},
                ).data,
            }
        )


class StoryMapAPIView(APIView):
    """Published story totals grouped by ISO 3166-1 alpha-2 country code."""

    def get(self, request):
        country_names = dict(COUNTRY_CHOICES)
        grouped_counts = list(
            Story.objects.published()
            .exclude(country="")
            .values("country")
            .annotate(stories_count=Count("id"))
            .order_by("-stories_count", "country")
        )
        countries = [
            {
                "code": item["country"],
                "name": country_names.get(item["country"], item["country"]),
                "stories_count": item["stories_count"],
            }
            for item in grouped_counts
        ]
        return Response(
            {
                "countries": countries,
                "total_stories": sum(item["stories_count"] for item in countries),
                "countries_count": len(countries),
                "max_stories_count": max(
                    (item["stories_count"] for item in countries),
                    default=0,
                ),
            }
        )


class AdminOverviewAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        total_views = Story.objects.aggregate(total=Sum("views")).get("total") or 0

        summary = {
            "stories": Story.objects.count(),
            "chapters": Chapter.objects.count(),
            "audios": Audio.objects.count(),
            "videos": Video.objects.count(),
            "users": User.objects.count(),
            "submissions_pending": Submission.objects.filter(status="pending").count(),
            "submissions_approved": Submission.objects.filter(status="approved").count(),
            "submissions_rejected": Submission.objects.filter(status="rejected").count(),
            "reviews": Review.objects.count(),
            "favorites": Favorite.objects.count(),
            "total_story_views": total_views,
            "active_readers": ReadingProgress.objects.values("user_id").distinct().count(),
            "active_listeners": AudioReadingProgress.objects.values("user_id").distinct().count(),
            "active_watchers": VideoWatchProgress.objects.values("user_id").distinct().count(),
        }

        most_read_stories_qs = (
            Story.objects.annotate(readers_count=Count("reading_progress", distinct=True))
            .order_by("-readers_count", "-views", "-id")[:8]
        )
        most_read_stories = [
            {
                "id": story.id,
                "title": story.title,
                "slug": story.slug,
                "cover_image": (
                    request.build_absolute_uri(story.cover_image_file.url)
                    if story.cover_image_file
                    else story.cover_image
                ),
                "readers_count": story.readers_count,
                "views": story.views,
                "rating": story.rating,
            }
            for story in most_read_stories_qs
        ]

        most_listened_audios_qs = (
            Audio.objects.select_related("story")
            .annotate(
                listeners_count=Count("audio_reading_progress", distinct=True),
                avg_progress=Avg("audio_reading_progress__progress"),
            )
            .order_by("-listeners_count", "-id")[:8]
        )
        most_listened_audios = [
            {
                "id": audio.id,
                "title": audio.title,
                "slug": audio.slug,
                "story_id": audio.story_id,
                "story_title": audio.story.title,
                "story_slug": audio.story.slug,
                "listeners_count": audio.listeners_count,
                "avg_progress": round(float(audio.avg_progress or 0), 3),
            }
            for audio in most_listened_audios_qs
        ]

        most_watched_videos_qs = (
            Video.objects.select_related("story")
            .annotate(
                watchers_count=Count("video_watch_progress", distinct=True),
                avg_progress=Avg("video_watch_progress__progress"),
            )
            .order_by("-watchers_count", "-id")[:8]
        )
        most_watched_videos = [
            {
                "id": video.id,
                "title": video.title,
                "slug": video.slug,
                "story_id": video.story_id,
                "story_title": video.story.title,
                "story_slug": video.story.slug,
                "watchers_count": video.watchers_count,
                "avg_progress": round(float(video.avg_progress or 0), 3),
            }
            for video in most_watched_videos_qs
        ]

        top_favorited_stories_qs = (
            Story.objects.annotate(favorites_count=Count("favorites", distinct=True))
            .order_by("-favorites_count", "-id")[:8]
        )
        top_favorited_stories = [
            {
                "id": story.id,
                "title": story.title,
                "slug": story.slug,
                "favorites_count": story.favorites_count,
            }
            for story in top_favorited_stories_qs
        ]

        top_rated_stories_qs = Story.objects.order_by("-rating", "-views", "-id")[:8]
        top_rated_stories = [
            {
                "id": story.id,
                "title": story.title,
                "slug": story.slug,
                "rating": story.rating,
                "views": story.views,
            }
            for story in top_rated_stories_qs
        ]

        return Response(
            {
                "summary": summary,
                "most_read_stories": most_read_stories,
                "most_listened_audios": most_listened_audios,
                "most_watched_videos": most_watched_videos,
                "top_favorited_stories": top_favorited_stories,
                "top_rated_stories": top_rated_stories,
            }
        )


class PromptSettingsAPIView(APIView):
    """Exposes the same PromptSettings singleton editable at /django-admin/
    (apps/story/admin.py's SingletonModelAdmin registration) through the
    custom admin panel too — both surfaces read/write the same one row via
    PromptSettings.get_solo()."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        return Response(PromptSettingsSerializer(PromptSettings.get_solo()).data)

    def patch(self, request):
        instance = PromptSettings.get_solo()
        serializer = PromptSettingsSerializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SearchStoryAPIView(APIView):
    def get(self, request):
        q = request.query_params.get("q", "").strip()
        sort = request.query_params.get("sort", "popular").lower()
        language = request.query_params.get("language", "").strip()

        stories = Story.objects.none()
        authors = Author.objects.none()
        chapters = Chapter.objects.none()

        if q:
            stories = (
                Story.objects.published()
                .select_related("author")
                .prefetch_related("genres", "audios", "videos", "tags")
                .filter(
                    Q(title__icontains=q)
                    | Q(about__icontains=q)
                    | Q(author__name__icontains=q)
                    | Q(genres__name__icontains=q)
                    | Q(tags__name__icontains=q)
                )
                .distinct()
            )
            authors = (
                Author.objects.filter(name__icontains=q)
                .annotate(
                    published_stories_count=Count(
                        "stories",
                        filter=published_story_q("stories"),
                        distinct=True,
                    )
                )
                .order_by("name", "id")
            )
            chapters = (
                Chapter.objects.filter(published_story_q("story"))
                .filter(Q(title__icontains=q) | Q(content__icontains=q))
                .select_related("story", "story__author")
                .order_by("story__title", "order")
            )

        if language and language.lower() != "all":
            stories = stories.filter(language=language)

        if sort == "recent":
            stories = stories.order_by("-site_published_date", "-id")
        elif sort == "rating":
            stories = stories.order_by("-rating", "-views", "-id")
        else:
            stories = stories.order_by("-views", "-rating", "-id")

        story_paginator = CataloguePagination()
        story_page = story_paginator.paginate_queryset(stories, request, view=self)
        story_data = StoryListSerializer(
            story_page, many=True, context={"request": request}
        ).data

        author_paginator = SearchAuthorPagination()
        author_page = author_paginator.paginate_queryset(authors, request, view=self)
        author_data = AuthorSerializer(
            author_page, many=True, context={"request": request}
        ).data

        chapter_paginator = SearchChapterPagination()
        chapter_page = chapter_paginator.paginate_queryset(chapters, request, view=self)
        chapter_data = ChapterSearchResultSerializer(
            chapter_page, many=True, context={"request": request, "query": q}
        ).data

        return Response(
            {
                "titles": story_paginator.get_response_data(story_data),
                "authors": author_paginator.get_response_data(author_data),
                "chapters": chapter_paginator.get_response_data(chapter_data),
            }
        )

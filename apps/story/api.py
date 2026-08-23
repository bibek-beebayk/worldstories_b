import mimetypes
import os
import re
import uuid

from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet
from rest_framework.views import APIView
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Sum, Avg, Count, F
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
from apps.stats.models import ReadingProgress, AudioReadingProgress
from apps.users.models import User
from apps.users.recommendations import recommend_because_finished
from .models import (
    Genre,
    Category,
    Story,
    Chapter,
    Audio,
    Author,
    Review,
    Favorite,
    Submission,
    StoryView,
    EpubImportJob,
    PromptSettings,
    Blog,
    StoryQueue,
    with_preferred_translation_only,
    published_story_q,
    STORY_TYPE_CHOICES,
    LANGUAGE_CHOICES,
)
from .epub_import_jobs import executor as epub_import_executor, run_epub_import
from .ai_generation_jobs import (
    executor as ai_generation_executor,
    run_generate_blog_excerpt,
    run_generate_field,
)
from .serializers import (
    GenreSerializer,
    CategorySerializer,
    AuthorSerializer,
    AuthorDetailSerializer,
    AdminGenreSerializer,
    AdminCategorySerializer,
    AdminAuthorSerializer,
    StoryListSerializer,
    FeaturedStorySerializer,
    StoryDetailSerializer,
    ChapterSerializer,
    AudioSerializer,
    ReviewSerializer,
    ReviewWriteSerializer,
    SubmissionSerializer,
    SubmissionListSerializer,
    StoryAdminSerializer,
    ChapterAdminSerializer,
    AudioAdminSerializer,
    SubmissionAdminSerializer,
    EpubImportJobSerializer,
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


class BlogPagination(PageNumberPagination):
    page_size = 12


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


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
        queryset = Story.objects.published().select_related("author").order_by("-id")
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
        queryset = Blog.objects.published().select_related("linked_story").order_by("-created_at")
        if self.request.query_params.get("sort") == "oldest":
            queryset = queryset.order_by("created_at")
        linked = self.request.query_params.get("linked_to_story")
        if linked == "true":
            queryset = queryset.filter(linked_story__isnull=False)
        elif linked == "false":
            queryset = queryset.filter(linked_story__isnull=True)
        linked_story_slug = self.request.query_params.get("linked_story")
        if linked_story_slug:
            queryset = queryset.filter(linked_story__slug=linked_story_slug)
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

    queryset = StoryQueue.objects.select_related("added_story").prefetch_related("genres", "categories").all()
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
        genre/category suggestion lists elsewhere in this form."""
        title = request.query_params.get("title", "").strip()
        if not title:
            return Response({"story_matches": [], "queue_matches": []})

        story_matches = [
            {"id": story.id, "title": story.title, "slug": story.slug, "is_published": story.is_published}
            for story in Story.objects.filter(title__istartswith=title).only(
                "id", "title", "slug", "is_published"
            )[:8]
        ]
        queue_matches = [
            {"id": item.id, "title": item.title, "author_name": item.author_name}
            for item in StoryQueue.objects.filter(title__istartswith=title, is_added=False).only(
                "id", "title", "author_name"
            )[:8]
        ]
        return Response({"story_matches": story_matches, "queue_matches": queue_matches})

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
            "original_published_year": queue_item.original_published_year,
            "original_published_month": queue_item.original_published_month,
            "original_published_day": queue_item.original_published_day,
        }
        if queue_item.author_name:
            author, _ = Author.objects.get_or_create(name=queue_item.author_name)
            story_data["author"] = author.id
        if queue_item.story_type:
            story_data["story_type"] = queue_item.story_type
        if queue_item.country:
            story_data["country"] = queue_item.country
        # cover_image is a plain URLField on Story (not an upload), so the
        # public-domain reference link copies straight across. epub_link/
        # pdf_link are intentionally NOT copied — see StoryQueue's docstring.
        if queue_item.cover_image_link:
            story_data["cover_image"] = queue_item.cover_image_link

        story_serializer = StoryAdminSerializer(data=story_data, context={"request": request})
        story_serializer.is_valid(raise_exception=True)
        story = story_serializer.save()

        queue_item.is_added = True
        queue_item.added_story = story
        queue_item.save(update_fields=["is_added", "added_story"])

        return Response(StoryQueueSerializer(queue_item).data, status=status.HTTP_201_CREATED)


class BlogAdminViewSet(ModelViewSet):
    queryset = Blog.objects.select_related("linked_story").all().order_by("-created_at")
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
        queryset = super().get_queryset()
        story_id = self.request.query_params.get("story")
        if story_id:
            queryset = queryset.filter(story_id=story_id).order_by("order", "id")
        return queryset


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


class GenreListAPIView(APIView):
    def get(self, request):
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
        serializer = GenreSerializer(genres, many=True)
        return Response(serializer.data)


class CategoryListAPIView(APIView):
    def get(self, request):
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
        serializer = CategorySerializer(categories, many=True)
        return Response(serializer.data)


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
                .prefetch_related("genres", "audios")
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


class CategoryAdminViewSet(ModelViewSet):
    queryset = Category.objects.all().order_by("name")
    serializer_class = AdminCategorySerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]


class AdminGenreListCreateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        genres = Genre.objects.all().order_by("name")
        serializer = AdminGenreSerializer(genres, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = AdminGenreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        genre = serializer.save()
        return Response(AdminGenreSerializer(genre).data, status=status.HTTP_201_CREATED)


class HomeDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.published().select_related("author").prefetch_related("genres", "audios")
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
            .prefetch_related("genres", "audios")
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
        base_qs = Story.objects.published().select_related("author").prefetch_related("genres", "audios")
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
        base_qs = Story.objects.published().select_related("author").prefetch_related("genres", "audios")
        trending_qs = base_qs.annotate(
            favorites_total=Count("favorites", distinct=True),
            reviews_total=Count("reviews", distinct=True),
        )
        story_type_counts = {
            item["story_type"]: item["stories_count"]
            for item in Story.objects.published()
            .values("story_type")
            .annotate(stories_count=Count("id"))
        }
        story_types = [
            {
                "value": value,
                "label": label,
                "stories_count": story_type_counts[value],
            }
            for value, label in STORY_TYPE_CHOICES
            if value in story_type_counts
        ]
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
                "story_types": story_types,
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


class AdminOverviewAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        total_views = Story.objects.aggregate(total=Sum("views")).get("total") or 0

        summary = {
            "stories": Story.objects.count(),
            "chapters": Chapter.objects.count(),
            "audios": Audio.objects.count(),
            "users": User.objects.count(),
            "submissions_pending": Submission.objects.filter(status="pending").count(),
            "submissions_approved": Submission.objects.filter(status="approved").count(),
            "submissions_rejected": Submission.objects.filter(status="rejected").count(),
            "reviews": Review.objects.count(),
            "favorites": Favorite.objects.count(),
            "total_story_views": total_views,
            "active_readers": ReadingProgress.objects.values("user_id").distinct().count(),
            "active_listeners": AudioReadingProgress.objects.values("user_id").distinct().count(),
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

        if q:
            stories = (
                Story.objects.published()
                .select_related("author")
                .prefetch_related("genres", "audios", "tags")
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

        return Response(
            {
                "titles": story_paginator.get_response_data(story_data),
                "authors": author_paginator.get_response_data(author_data),
            }
        )

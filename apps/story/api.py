import mimetypes
import os
import random
import re
import uuid

from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet
from rest_framework.views import APIView
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, Sum, Avg, Count, Exists, F, FloatField, IntegerField, Min, OuterRef, Prefetch, Subquery, When, Value, CharField
from django.db.models import BooleanField, ExpressionWrapper, Max, Q
from django.db.models.functions import Coalesce, Lower, NullIf
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from django.core.cache import cache
from datetime import date, timedelta
from storages.backends.s3 import S3Storage
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.filters import SearchFilter

from apps.story.filters import StoryFilter
from apps.stats.models import (
    AnalyticsEvent,
    AudioReadingProgress,
    QuickReadProgress,
    ReadingProgress,
    StoryCompletion,
    VideoWatchProgress,
)
from apps.users.models import User
from apps.stats.journeys import journey_progress, journeys_for_reader
from apps.users.recommendations import (
    completion_recommendations,
    recommend_because_finished,
    recommend_stories_for,
)
from .home_metrics import get_home_stats
from .author_merge import merge_authors
from .home_sections import resolve_home_sections
from .models import (
    HomeSection,
    default_story_type_id,
    Genre,
    Category,
    Story,
    Chapter,
    Audio,
    Audiobook,
    AudiobookSeries,
    AudioTranscriptCue,
    audiobook_published_q,
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
    BlogTopic,
    StoryQueue,
    StoryType,
    VideoType,
    VideoSeries,
    DailyStory,
    Tag,
    Theme,
    with_preferred_translation_only,
    published_story_q,
    published_blog_q,
    LANGUAGE_CHOICES,
    COUNTRY_CHOICES,
    Mood,
    StoryJourney,
    StoryJourneyItem,
    StoryMood,
    StoryReaction,
    HeroTemplate,
)
from .epub_import_jobs import executor as epub_import_executor, run_epub_import
from .rich_text import rich_text_has_content
from .story_map import REGION_ORDER, story_map_region
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
    run_suggest_blog_topics,
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
    CARD_COVER_SIZE,
    AdminMoodSerializer,
    AdminStoryJourneyItemSerializer,
    AdminStoryJourneySerializer,
    AdminHeroTemplateSerializer,
    serialize_hero,
    HomeSectionAdminSerializer,
    AdminStoryMoodSerializer,
    get_cover_image_url,
    AdminThemeSerializer,
    StoryTypeSerializer,
    AdminStoryTypeSerializer,
    AdminVideoTypeSerializer,
    PublicVideoTypeSerializer,
    AuthorSerializer,
    AuthorDetailSerializer,
    AdminGenreSerializer,
    AdminCategorySerializer,
    AdminAuthorSerializer,
    StoryListSerializer,
    StoryCardWithSummarySerializer,
    FeaturedStorySerializer,
    FeaturedStoryAdminSerializer,
    DailyStorySerializer,
    DailyStoryAdminSerializer,
    FeaturedVideoAdminSerializer,
    ChapterSearchResultSerializer,
    StoryDetailSerializer,
    ChapterSerializer,
    AudioSerializer,
    AudiobookDetailSerializer,
    AudiobookSeriesSerializer,
    PublicAudiobookSerializer,
    AudioTranscriptCueSerializer,
    ReadAlongSerializer,
    VideoSerializer,
    PublicVideoSerializer,
    VideoSeriesSerializer,
    ReviewSerializer,
    ReviewWriteSerializer,
    SubmissionSerializer,
    SubmissionListSerializer,
    StoryAdminSerializer,
    ChapterAdminSerializer,
    AudioAdminSerializer,
    AudiobookSeriesAdminSerializer,
    AudiobookTrackAdminSerializer,
    AudiobookAdminListSerializer,
    AudiobookAdminDetailSerializer,
    VideoAdminSerializer,
    SubmissionAdminSerializer,
    EpubImportJobSerializer,
    BookFetchJobSerializer,
    PromptSettingsSerializer,
    BlogSerializer,
    BlogAdminSerializer,
    BlogAdminListSerializer,
    BlogListSerializer,
    BlogCardSerializer,
    BlogTopicSerializer,
    StoryQueueSerializer,
)
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError
from core.libs.pagination import PageNumberPagination
from core.libs.site_sources import is_nepalikatha_request


VIEW_DEDUPE_WINDOW = timedelta(hours=24)

# How many ranked stories "Surprise Me" picks from. Wide enough that pressing
# it twice rarely repeats, narrow enough that every result is one the ranking
# would have been happy to recommend anyway.
SURPRISE_SHORTLIST = 40

# Named crawlers/link-preview scrapers, plus the generic markers that catch the
# long tail nobody maintains a list for (unknown search bots, scrapers, HTTP
# libraries hitting the API directly). Analytics writes are all explicit
# browser-originated calls now, so anything non-interactive reaching them is
# noise by definition — see _register_view and AnalyticsEventCreateAPIView.
BOT_USER_AGENT_PATTERN = re.compile(
    r"facebookexternalhit|Facebot|Twitterbot|Slackbot|Slack-ImgProxy|Discordbot|WhatsApp|"
    r"TelegramBot|LinkedInBot|Pinterest|redditbot|SkypeUriPreview|Applebot|Googlebot|"
    r"bingbot|DuckDuckBot|YandexBot|W3C_Validator|vkShare|"
    r"\bbot\b|[-_ ]bot|bot/|crawl|spider|slurp|scrap|headless|phantomjs|fetcher|"
    r"python-requests|python-urllib|aiohttp|httpx|node-fetch|undici|axios|curl/|wget/|"
    r"go-http-client|okhttp|java/|libwww|lighthouse|pagespeed|gtmetrix|semrush|ahrefs|mj12",
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
    user_agent = request.META.get("HTTP_USER_AGENT", "").strip()
    # A blank User-Agent is never an interactive browser — every browser sends
    # one, so an absent header means a script or a deliberately stripped client.
    if not user_agent:
        return True
    return bool(BOT_USER_AGENT_PATTERN.search(user_agent))


def is_analytics_excluded_user(request):
    """Superusers run the site as operators — previewing a story, checking a
    reader, spot-checking a blog post. None of that is audience behaviour, so
    it must never reach any counter the admin dashboard reads back."""
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and (user.is_superuser or user.is_staff))


def is_untracked_request(request, *, companion=False):
    """Single gate for every analytics write: real, human, non-operator traffic
    only. Main-site writers also reject companion-site origins; the dedicated
    companion endpoint opts into its own stream while retaining bot/operator exclusion."""
    return (is_bot_request(request) or is_analytics_excluded_user(request)
            or (not companion and is_nepalikatha_request(request)))


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

    def get_page_size(self, request):
        if request.query_params.get("page_size") == "all":
            return 500
        return super().get_page_size(request)


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

    def _base_queryset(self):
        from .analytics_api import audience_events

        audio_exists = Audio.objects.filter(story__author_id=OuterRef("pk")).filter(
            published_story_q("story"), audio_file__isnull=False
        ).exclude(audio_file="")
        video_exists = Video.objects.filter(story__author_id=OuterRef("pk")).filter(published_story_q("story"))
        read_along_exists = audio_exists.exclude(transcript="")
        cutoff = timezone.now() - timedelta(days=30)
        popular = (
            audience_events(
                AnalyticsEvent.objects.filter(
                    story__author_id=OuterRef("pk"),
                    created_at__gte=cutoff,
                    event_type__in=[
                        AnalyticsEvent.EVENT_READING_SESSION,
                        AnalyticsEvent.EVENT_LISTENING_SESSION,
                        AnalyticsEvent.EVENT_WATCHING_SESSION,
                    ],
                )
            )
            .order_by()
            .values("story__author_id")
            .annotate(total=Count("visitor_id", distinct=True))
            .values("total")[:1]
        )
        return Author.objects.annotate(
            published_stories_count=Count("stories", filter=published_story_q("stories"), distinct=True),
            directory_name=Case(When(sort_name="", then=F("name")), default=F("sort_name"), output_field=CharField()),
            popular_readers=Coalesce(Subquery(popular), 0),
            has_audio_format=Exists(audio_exists),
            has_video_format=Exists(video_exists),
            has_read_along_format=Exists(read_along_exists),
        )

    def get_queryset(self):
        queryset = self._base_queryset()
        if self.request.query_params.get("include_empty") != "1":
            queryset = queryset.filter(published_stories_count__gt=0, is_house=False)
        query = (self.request.query_params.get("q") or "").strip()
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(known_for__icontains=query))
        letter = (self.request.query_params.get("letter") or "").strip()[:1]
        if letter:
            queryset = queryset.filter(
                Q(sort_name__istartswith=letter) | Q(sort_name="", name__istartswith=letter)
            )
        country = self.request.query_params.get("country")
        if country:
            queryset = queryset.filter(country=country)
        era = self.request.query_params.get("era")
        if era:
            queryset = queryset.filter(era=era)
        genre = self.request.query_params.get("genre")
        if genre:
            queryset = queryset.filter(stories__genres__slug=genre, stories__is_published=True).distinct()
        sort = self.request.query_params.get("sort", "az")
        if sort == "titles":
            return queryset.order_by("-published_stories_count", "directory_name", "id")
        if sort == "popular":
            return queryset.order_by("-popular_readers", "-published_stories_count", "directory_name", "id")
        return queryset.order_by("directory_name", "id")

    def get_object(self):
        value = self.kwargs.get(self.lookup_field or "pk")
        queryset = self.filter_queryset(self.get_queryset())
        lookup = Q(pk=int(value)) if str(value).isdigit() else Q(slug=value)
        return get_object_or_404(queryset, lookup)

    @action(detail=False, methods=["get"])
    def overview(self, request):
        cache_key = "authors-overview-v1"
        payload = cache.get(cache_key)
        if payload is not None:
            return Response(payload)
        listed = self._base_queryset().filter(published_stories_count__gt=0, is_house=False)
        featured_qs = listed.order_by("-popular_readers", "-published_stories_count", "directory_name", "id")
        if not listed.filter(popular_readers__gt=0).exists():
            featured_qs = listed.order_by("-published_stories_count", "directory_name", "id")
        featured = list(featured_qs[:5])
        letters = sorted({((item.sort_name or item.name).strip()[:1].upper()) for item in listed if (item.sort_name or item.name).strip()})
        countries = list(
            listed.exclude(country="").values("country").annotate(count=Count("id")).order_by("country")
        )
        country_labels = dict(COUNTRY_CHOICES)
        for item in countries:
            item["label"] = country_labels.get(item["country"], item["country"])
        eras = list(listed.exclude(era="").values("era").annotate(count=Count("id")).order_by("era"))
        traditional_count = Story.objects.filter(published_story_q()).filter(
            Q(author__is_traditional=True) | Q(author__isnull=True)
        ).count()
        payload = {
            "featured": AuthorSerializer(featured, many=True, context=self.get_serializer_context()).data,
            "letters": letters,
            "countries": countries,
            "eras": eras,
            "traditional_count": traditional_count,
        }
        cache.set(cache_key, payload, 60 * 15)
        return Response(payload)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AuthorDetailSerializer
        return AuthorSerializer


class VideoViewSet(ReadOnlyModelViewSet):
    """Public Watch catalogue: standalone videos plus videos of published
    stories, newest first. Detail adds a `suggested` list for the player page."""

    serializer_class = PublicVideoSerializer
    filter_backends = [SearchFilter]
    search_fields = ["display_title", "title", "story__title"]
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = (
            Video.objects.select_related("story", "story__author", "video_type", "series")
            .prefetch_related("story__audios", "story__videos__series")
            .filter(Q(story__isnull=True) | published_story_q("story"))
            .order_by("-created_at", "-id")
        )
        # Only narrows the list; detail/suggestions must still resolve any video.
        video_type = self.request.query_params.get("video_type")
        if self.action == "list" and video_type and video_type.isdigit():
            queryset = queryset.filter(video_type_id=int(video_type))
        if self.action == "list":
            series = self.request.query_params.get("series")
            if series:
                queryset = queryset.filter(series__slug=series)

            length = self.request.query_params.get("length")
            if length == "short":
                queryset = queryset.filter(duration_seconds__lt=300)
            elif length == "mid":
                queryset = queryset.filter(duration_seconds__gte=300, duration_seconds__lt=1200)
            elif length == "long":
                queryset = queryset.filter(duration_seconds__gte=1200)

            query = (self.request.query_params.get("q") or "").strip()
            if query:
                queryset = queryset.filter(
                    Q(display_title__icontains=query) | Q(title__icontains=query) | Q(story__title__icontains=query)
                )

            sort = self.request.query_params.get("sort", "newest")
            if sort == "shortest":
                queryset = queryset.filter(duration_seconds__isnull=False).order_by("duration_seconds", "id")
            elif sort == "longest":
                queryset = queryset.filter(duration_seconds__isnull=False).order_by("-duration_seconds", "id")
            elif sort == "most_watched":
                cutoff = timezone.now() - timedelta(days=30)
                watched = (
                    AnalyticsEvent.objects.filter(
                        video_id=OuterRef("pk"),
                        event_type=AnalyticsEvent.EVENT_WATCHING_SESSION,
                        created_at__gte=cutoff,
                        is_suspected_bot=False,
                    )
                    .filter(Q(user__isnull=True) | Q(user__is_staff=False, user__is_superuser=False))
                    .order_by()
                    .values("video_id")
                    .annotate(total=Count("visitor_id", distinct=True))
                    .values("total")[:1]
                )
                queryset = queryset.annotate(watchers_30d=Coalesce(Subquery(watched), 0)).order_by(
                    "-watchers_30d", "-created_at", "-id"
                )
        return queryset

    @action(detail=False, methods=["get"])
    def overview(self, request):
        cache_key = "watch-overview-anonymous-v1"
        anonymous = cache.get(cache_key)
        if anonymous is None:
            base = self.get_queryset().order_by("-created_at", "-id")
            featured = base.filter(featured_rank__isnull=False).order_by("featured_rank", "id").first()
            if featured is None:
                featured = base.filter(Q(series__layout="wide") | Q(aspect_ratio="16:9")).first()
            shelves = []
            for series in VideoSeries.objects.filter(is_active=True).order_by("order", "name"):
                videos = list(base.filter(series=series).order_by("order", "-created_at", "-id")[:13])
                shelves.append(
                    {
                        "series": VideoSeriesSerializer(series).data,
                        "videos": PublicVideoSerializer(videos, many=True, context=self.get_serializer_context()).data,
                    }
                )
            anonymous = {
                "featured": PublicVideoSerializer(featured, context=self.get_serializer_context()).data if featured else None,
                "shelves": shelves,
                "count": base.count(),
            }
            cache.set(cache_key, anonymous, 60 * 15)

        continue_watching = []
        if request.user.is_authenticated:
            rows = (
                VideoWatchProgress.objects.filter(user=request.user, progress__gt=0, progress__lt=1)
                .select_related("video", "video__story", "video__story__author", "video__video_type", "video__series")
                .prefetch_related("video__story__audios", "video__story__videos__series")
                .order_by("-updated_at")[:6]
            )
            for progress in rows:
                duration = progress.duration_seconds or progress.video.duration_seconds or 0
                continue_watching.append(
                    {
                        "video": PublicVideoSerializer(progress.video, context=self.get_serializer_context()).data,
                        "progress_percent": round(max(0, min(progress.progress, 1)) * 100),
                        "seconds_left": max(0, round(duration - progress.position_seconds)),
                    }
                )
        return Response({**anonymous, "continue_watching": continue_watching})

    def retrieve(self, request, *args, **kwargs):
        video = self.get_object()
        context = self.get_serializer_context()
        # Same-story siblings first (playback order), then the newest others.
        siblings = list(
            self.get_queryset().filter(story=video.story).exclude(pk=video.pk).order_by("order", "id")[:12]
        ) if video.story_id else []
        others = list(
            self.get_queryset().exclude(pk=video.pk).exclude(pk__in=[v.pk for v in siblings])[: 12 - len(siblings)]
        )
        return Response(
            {
                **PublicVideoSerializer(video, context=context).data,
                "suggested": PublicVideoSerializer(siblings + others, many=True, context=context).data,
            }
        )


class VideoTypeViewSet(ReadOnlyModelViewSet):
    """Public video categories for the Watch page filter — only types that
    have at least one watchable video."""

    serializer_class = PublicVideoTypeSerializer
    pagination_class = None

    def get_queryset(self):
        return (
            VideoType.objects.annotate(
                videos_count=Count(
                    "videos",
                    filter=Q(videos__story__isnull=True) | published_story_q("videos__story"),
                )
            )
            .filter(videos_count__gt=0)
            .order_by("name")
        )


class VideoSeriesViewSet(ReadOnlyModelViewSet):
    serializer_class = VideoSeriesSerializer
    pagination_class = None
    queryset = VideoSeries.objects.filter(is_active=True).order_by("order", "name")


def _stream_audio_file_response(request, audio):
    """Serve an Audio file with the byte-range semantics required by Safari."""
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


class AudiobookSeriesViewSet(ReadOnlyModelViewSet):
    serializer_class = AudiobookSeriesSerializer
    pagination_class = None
    queryset = AudiobookSeries.objects.filter(is_active=True).order_by("order", "name")


class AudiobookViewSet(ReadOnlyModelViewSet):
    """Public Audiobooks catalogue: standalone audiobooks plus audiobooks of
    published stories that have at least one playable audio track."""

    lookup_field = "slug"
    http_method_names = ["get", "head", "options"]
    permission_classes = [AllowAny]
    pagination_class = CataloguePagination

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AudiobookDetailSerializer
        return PublicAudiobookSerializer

    def _base_queryset(self):
        has_playable_track = Exists(
            Audio.objects.filter(
                audiobook=OuterRef("pk"),
                audio_file__isnull=False,
            ).exclude(audio_file="")
        )
        return (
            Audiobook.objects.published()
            .filter(has_playable_track)
            .select_related("story", "story__author", "author", "series")
            .prefetch_related(
                "genres",
                "story__genres",
                Prefetch(
                    "tracks",
                    queryset=Audio.objects.annotate(
                        _has_timed_cues=Exists(
                            AudioTranscriptCue.objects.filter(audio=OuterRef("pk"))
                        )
                    ).order_by("order"),
                ),
            )
        )

    @staticmethod
    def _order_by_popular(queryset):
        return queryset.annotate(
            _ab_listeners=Count("audio_reading_progress__user", distinct=True),
            _pop_views=Coalesce("story__views", Value(0)),
            _pop_rating=Coalesce("story__rating", Value(0.0), output_field=FloatField()),
        ).order_by("-_ab_listeners", "-_pop_views", "-_pop_rating", "-created_at", "-id")

    def get_queryset(self):
        queryset = self._base_queryset().order_by("-created_at", "-id")
        if self.action != "list":
            return queryset

        params = self.request.query_params
        q = (params.get("q") or "").strip()
        if q:
            queryset = queryset.filter(
                Q(title__icontains=q)
                | Q(display_title__icontains=q)
                | Q(narrator__icontains=q)
                | Q(author__name__icontains=q)
                | Q(story__author__name__icontains=q)
                | Q(story__title__icontains=q)
            ).distinct()

        genre = (params.get("genre") or "").strip()
        if genre and genre.lower() != "all":
            has_own_genres = Exists(
                Audiobook.genres.through.objects.filter(audiobook_id=OuterRef("pk"))
            )
            if genre.isdigit():
                gid = int(genre)
                queryset = queryset.filter(
                    Q(genres__id=gid) | (~has_own_genres & Q(story__genres__id=gid))
                ).distinct()
            else:
                queryset = queryset.filter(
                    Q(genres__slug=genre) | (~has_own_genres & Q(story__genres__slug=genre))
                ).distinct()

        language = (params.get("language") or "").strip().lower()
        if language and language != "all":
            lang_q = Q(language=language) | (Q(language="") & Q(story__language=language))
            if language == "en":
                lang_q |= Q(language="", story__isnull=True)
            queryset = queryset.filter(lang_q)

        series = (params.get("series") or "").strip()
        if series:
            queryset = queryset.filter(series__slug=series)

        narrator = (params.get("narrator") or "").strip()
        if narrator:
            queryset = queryset.filter(narrator__icontains=narrator)

        if (params.get("has_read_along") or "").strip().lower() == "true":
            has_ra = Exists(
                Audio.objects.filter(
                    audiobook=OuterRef("pk"),
                    audio_file__isnull=False,
                    transcript__isnull=False,
                )
                .exclude(audio_file="")
                .exclude(transcript="")
            )
            queryset = queryset.filter(has_ra)

        audio_length = (params.get("audio_length") or "").strip().lower()
        if audio_length == "short":
            queryset = queryset.filter(
                total_duration_seconds__isnull=False,
                total_duration_seconds__gt=0,
                total_duration_seconds__lt=1800,
            )
        elif audio_length == "medium":
            queryset = queryset.filter(
                total_duration_seconds__isnull=False,
                total_duration_seconds__gte=1800,
                total_duration_seconds__lt=7200,
            )
        elif audio_length == "long":
            queryset = queryset.filter(
                total_duration_seconds__isnull=False,
                total_duration_seconds__gte=7200,
                total_duration_seconds__lt=18000,
            )
        elif audio_length == "epic":
            queryset = queryset.filter(
                total_duration_seconds__isnull=False,
                total_duration_seconds__gte=18000,
            )

        max_minutes = (params.get("max_minutes") or "").strip()
        if max_minutes.isdigit():
            queryset = queryset.filter(
                total_duration_seconds__isnull=False,
                total_duration_seconds__gt=0,
                total_duration_seconds__lte=int(max_minutes) * 60,
            )

        sort = (params.get("sort") or "newest").strip().lower()
        if sort == "title":
            queryset = queryset.annotate(
                _eff_title=Lower(
                    Coalesce(
                        NullIf("display_title", Value("")),
                        NullIf("title", Value("")),
                        F("story__title"),
                        Value(""),
                    )
                )
            ).order_by("_eff_title", "id")
        elif sort == "shortest":
            queryset = queryset.order_by(F("total_duration_seconds").asc(nulls_last=True), "id")
        elif sort == "longest":
            queryset = queryset.order_by(F("total_duration_seconds").desc(nulls_last=True), "id")
        elif sort == "popular":
            queryset = self._order_by_popular(queryset)
        else:
            queryset = queryset.order_by("-created_at", "-id")

        return queryset

    @action(detail=False, methods=["get"])
    def overview(self, request):
        cache_key = "audiobook-overview-v1"
        anonymous = cache.get(cache_key)
        ctx = self.get_serializer_context()
        if anonymous is None:
            base = self._base_queryset()
            has_cover_q = (
                (Q(cover_image_file__isnull=False) & ~Q(cover_image_file=""))
                | (Q(story__cover_image_file__isnull=False) & ~Q(story__cover_image_file=""))
                | (Q(story__cover_image__isnull=False) & ~Q(story__cover_image=""))
            )
            featured = self._order_by_popular(base.filter(has_cover_q)).first()
            shelves = []
            for series in AudiobookSeries.objects.filter(is_active=True).order_by("order", "name"):
                items = list(
                    base.filter(series=series).order_by("series_order", "-created_at", "-id")[:13]
                )
                shelves.append(
                    {
                        "series": AudiobookSeriesSerializer(series).data,
                        "audiobooks": PublicAudiobookSerializer(items, many=True, context=ctx).data,
                    }
                )
            anonymous = {
                "featured": PublicAudiobookSerializer(featured, context=ctx).data if featured else None,
                "shelves": shelves,
                "count": base.count(),
            }
            cache.set(cache_key, anonymous, 60 * 15)

        continue_listening = []
        if request.user.is_authenticated:
            ab_rows = list(
                AudioReadingProgress.objects.filter(
                    user=request.user,
                    audiobook__isnull=False,
                    progress__gt=0,
                    progress__lt=1,
                )
                .filter(audiobook_published_q("audiobook"))
                .select_related(
                    "audio",
                    "audiobook",
                    "audiobook__story",
                    "audiobook__story__author",
                    "audiobook__author",
                    "audiobook__series",
                )
                .prefetch_related(
                    "audiobook__genres",
                    "audiobook__story__genres",
                    "audiobook__tracks",
                )
                .order_by("-updated_at")
            )
            seen_audiobooks = set()
            for row in ab_rows:
                if row.audiobook_id in seen_audiobooks:
                    continue
                seen_audiobooks.add(row.audiobook_id)
                continue_listening.append(
                    {
                        "audiobook": PublicAudiobookSerializer(row.audiobook, context=ctx).data,
                        "audio_id": row.audio_id,
                        "track_slug": row.audio.slug if row.audio_id else None,
                        "track_title": row.audio.title if row.audio_id else None,
                        "position_seconds": max(0.0, row.position_seconds),
                        "progress_percent": round(max(0.0, min(row.progress, 1.0)) * 100),
                        "updated_at": row.updated_at,
                    }
                )
                if len(continue_listening) >= 6:
                    break

        return Response({**anonymous, "continue_listening": continue_listening})

    @action(detail=False, methods=["get"])
    def stats(self, request):
        base = self._base_queryset()
        count = base.count()
        total_seconds = base.aggregate(total=Sum("total_duration_seconds")).get("total") or 0
        unknown_duration_count = base.filter(
            Q(total_duration_seconds__isnull=True) | Q(total_duration_seconds__lte=0)
        ).count()
        return Response(
            {
                "count": count,
                "total_seconds": float(total_seconds),
                "unknown_duration_count": unknown_duration_count,
            }
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"read-along/(?P<audio_slug>[^/.]+)",
    )
    def read_along(self, request, slug=None, audio_slug=None):
        audiobook = self.get_object()
        tracks = list(
            audiobook.tracks.prefetch_related("transcript_cues").order_by("order", "id")
        )
        audio = next((t for t in tracks if t.slug == audio_slug), None)
        if audio is None:
            raise Http404("Audio track not found.")

        from .rich_text import sanitize_reader_html

        audio_file_url = None
        if audio.audio_file:
            try:
                audio_file_url = audio.audio_file.url
            except (AttributeError, ValueError):
                audio_file_url = None
            if audio_file_url:
                audio_file_url = request.build_absolute_uri(audio_file_url)

        stream_url = None
        if audio.audio_file:
            stream_path = f"/api/audiobooks/{audiobook.slug}/tracks/{audio.slug}/stream/"
            stream_url = request.build_absolute_uri(stream_path)

        has_transcript = rich_text_has_content(audio.transcript)
        transcript_html = sanitize_reader_html(audio.transcript) if has_transcript else ""
        cue_payload = [
            {
                "id": cue.id,
                "start_seconds": round(cue.start_ms / 1000, 3),
                "end_seconds": round(cue.end_ms / 1000, 3),
                "text": cue.text,
            }
            for cue in audio.transcript_cues.all()
        ]
        synchronized = has_transcript and bool(cue_payload)
        author = audiobook.effective_author()
        nav_tracks = [
            {
                "id": t.id,
                "title": t.title,
                "slug": t.slug,
                "order": t.order,
                "has_transcript": rich_text_has_content(t.transcript),
                "read_along_available": bool(t.audio_file) and rich_text_has_content(t.transcript),
                "transcript_synchronized": bool(t.transcript_cues.all()),
            }
            for t in tracks
        ]
        idx = next((i for i, t in enumerate(tracks) if t.id == audio.id), 0)
        prev_audio = nav_tracks[idx - 1] if idx > 0 else None
        next_audio = nav_tracks[idx + 1] if idx + 1 < len(nav_tracks) else None

        return Response(
            {
                "audiobook": {
                    "id": audiobook.id,
                    "title": audiobook.effective_title(),
                    "slug": audiobook.slug,
                    "story_slug": audiobook.story.slug if audiobook.story_id else None,
                    "narrator": audiobook.narrator,
                    "language": audiobook.effective_language(),
                    "cover_image": audiobook.effective_cover(request=request, size="1280x1280"),
                    "author": {"id": author.id, "name": author.name} if author else None,
                },
                "story": {
                    "id": audiobook.story_id or audiobook.id,
                    "title": audiobook.effective_title(),
                    "slug": audiobook.slug,
                    "story_slug": audiobook.story.slug if audiobook.story_id else None,
                    "language": audiobook.effective_language(),
                    "story_type": audiobook.story.story_type.name if audiobook.story_id else "Audiobook",
                    "cover_image": audiobook.effective_cover(request=request, size="1280x1280"),
                    "author": {"id": author.id, "name": author.name} if author else None,
                },
                "audio": {
                    "id": audio.id,
                    "title": audio.title,
                    "slug": audio.slug,
                    "order": audio.order,
                    "audio_file": audio_file_url,
                    "stream_url": stream_url,
                    "duration_seconds": audio.duration_seconds,
                    "download_size_bytes": audio.file_size_bytes,
                    "has_transcript": has_transcript,
                    "read_along_available": bool(audio.audio_file) and has_transcript,
                    "transcript_synchronized": synchronized,
                },
                "transcript": {
                    "html": transcript_html,
                    "cues": cue_payload,
                    "synchronized": synchronized,
                    "read_along_offset_ms": audio.read_along_offset_ms,
                },
                "navigation": {
                    "previous": prev_audio,
                    "next": next_audio,
                    "audios": nav_tracks,
                },
            }
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"tracks/(?P<audio_slug>[^/.]+)/stream",
    )
    def stream_track(self, request, slug=None, audio_slug=None):
        audiobook = self.get_object()
        audio = get_object_or_404(Audio, audiobook=audiobook, slug=audio_slug)
        if not audio.audio_file:
            raise Http404("Audio file not found.")
        return _stream_audio_file_response(request, audio)


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


def wants_nepali_site(request) -> bool:
    """Is this request asking for the Nepali companion site's subset?

    One definition, shared by the story list and the taxonomy directories, so
    the three endpoints cannot drift on what counts as "true". Anything other
    than a recognised truthy value means "no filter" rather than "empty", so a
    typo shows the full catalogue instead of an apparently-broken site.
    """
    return (
        request.query_params.get("show_in_nepali_site", "").strip().lower()
        in {"true", "1"}
    )


def taxonomy_directory_queryset(model, request):
    """Public Genre/Category directory queryset.

    Counts only currently-visible stories and hides taxonomies with none —
    unchanged behaviour. When the Nepali site asks for its own subset, both
    the count *and* the visibility test narrow to flagged stories, so the
    filter dropdown offers only genres that have something behind them and the
    numbers beside them are the numbers that site will actually show.
    """
    story_filter = published_story_q("stories")
    if wants_nepali_site(request):
        story_filter &= Q(stories__show_in_nepali_site=True)
    return (
        model.objects.annotate(
            published_stories_count=Count("stories", filter=story_filter, distinct=True)
        )
        .filter(published_stories_count__gt=0)
        .order_by("name")
    )


class GenreViewSet(ReadOnlyModelViewSet):
    """Public genre directory — list behavior unchanged from the old
    GenreListAPIView it replaces, plus a slug-keyed retrieve backing the new
    /genre/<slug> SEO landing pages. Same shape as TagViewSet."""

    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        return taxonomy_directory_queryset(Genre, self.request)

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
        return taxonomy_directory_queryset(Category, self.request)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CategoryDetailSerializer
        return CategorySerializer


class _NoFormatOverrideSettings:
    """api_settings, except DRF's `?format=` renderer override is off."""

    def __getattr__(self, name):
        from rest_framework.settings import api_settings

        return None if name == "URL_FORMAT_OVERRIDE" else getattr(api_settings, name)


class StoryContentNegotiation(DefaultContentNegotiation):
    """The story endpoints answer JSON only, and `format` is a real filter here
    (`?format=listen`). DRF would otherwise read it as a renderer name and 404."""

    settings = _NoFormatOverrideSettings()


class StoryViewSet(ReadOnlyModelViewSet):
    content_negotiation_class = StoryContentNegotiation

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
            .select_related("author", "story_type")
            .prefetch_related(
                "genres",
                "categories",
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
        if self.action == "retrieve":
            # Story detail renders tags/themes as badges that link out only when
            # the tag/theme has other published stories — annotate each with its
            # published-story count so TagSerializer/ThemeSerializer read the
            # annotation instead of one COUNT query per badge. A correlated
            # Subquery rather than Count("stories", ...): the prefetch already
            # joins the same M2M and restricts it to this story, which would
            # collapse a plain relation Count to 1.
            def _published_count(m2m_name):
                return Coalesce(
                    Subquery(
                        Story.objects.filter(published_story_q(), **{m2m_name: OuterRef("pk")})
                        .order_by()
                        .values(m2m_name)
                        .annotate(n=Count("pk"))
                        .values("n"),
                        output_field=IntegerField(),
                    ),
                    0,
                )

            queryset = queryset.prefetch_related(
                Prefetch(
                    "tags",
                    queryset=Tag.objects.annotate(published_stories_count=_published_count("tags")),
                ),
                Prefetch(
                    "themes",
                    queryset=Theme.objects.annotate(published_stories_count=_published_count("themes")),
                ),
                Prefetch(
                    "audiobooks",
                    queryset=Audiobook.objects.published()
                    .filter(
                        Exists(
                            Audio.objects.filter(
                                audiobook=OuterRef("pk"),
                                audio_file__isnull=False,
                            ).exclude(audio_file="")
                        )
                    )
                    .order_by("id"),
                    to_attr="published_audiobooks",
                ),
            )
        if self.action == "list":
            # When the Nepali site asks for its own catalogue, the candidate
            # set for translation collapsing has to be narrowed the same way —
            # otherwise a flagged story whose group also holds an unflagged
            # English edition loses to it and vanishes. See
            # with_preferred_translation_only's docstring.
            nepali_only = wants_nepali_site(self.request)
            queryset = with_preferred_translation_only(
                queryset,
                preferred_language=self.request.query_params.get("language"),
                candidate_filter=Q(show_in_nepali_site=True) if nepali_only else None,
            )
            # StoryListSerializer reads these; keeps the browse grid off a
            # per-row .count()/.exists() (see StoryQuerySet.for_card_list).
            annotations = {
                "_reviews_count": Count("reviews", distinct=True),
                "_favorites_count": Count("favorites", distinct=True),
                # "Read" format badge: there is text to read here (chapters or
                # an uploaded EPUB/PDF) — the same rule as ?format=read.
                "_has_text": ExpressionWrapper(
                    Q(Exists(Chapter.objects.filter(story=OuterRef("pk"))))
                    | Q(pdf_file__gt="")
                    | Q(epub_file__gt=""),
                    output_field=BooleanField(),
                ),
            }
            if self.request.user.is_authenticated:
                annotations["_is_favorite"] = Exists(
                    Favorite.objects.filter(
                        story=OuterRef("pk"), user=self.request.user
                    )
                )
                if (self.request.query_params.get("has_summary") or "").lower() == "true":
                    annotations["_quick_read_progress"] = Subquery(
                        QuickReadProgress.objects.filter(
                            user=self.request.user, story=OuterRef("pk")
                        ).values("progress")[:1],
                        output_field=FloatField(),
                    )
            queryset = queryset.annotate(**annotations)
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
        page or re-fetching for chapter navigation doesn't inflate the count.

        Only ever reached from the explicit ``POST /stories/<slug>/view/`` beacon
        the story page fires from the browser — never from ``retrieve``. Under SSR
        the detail GET is issued by the render server, not the visitor: it carries
        the render host's IP and User-Agent, which both defeats the bot filter
        (a crawler's request arrives wearing the server's identity) and collapses
        the IP dedupe (every visitor worldwide sharing a couple of IPs, so all but
        the first view of a story in 24h was being dropped)."""
        if is_untracked_request(request):
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

    # No retrieve() override: it existed only to call _register_view, which
    # the browser-fired beacon below now owns.
    @action(
        detail=True,
        methods=["get", "post", "delete"],
        url_path="reactions",
        permission_classes=[AllowAny],
    )
    def reactions(self, request, slug=None):
        """Totals for a story, and this reader's own reaction.

        GET is open: §10.4 says anonymous readers may see the totals, and a
        panel showing "231 loved it" is worth seeing before you have an
        account. POST and DELETE need one, since a reaction belongs to someone.

        POST with the reaction you already have removes it — the tap that set
        it is the tap that unsets it, which is what a reader expects from a
        control that looks like a toggle. Any other value replaces it, so
        changing your mind updates the row rather than adding a second (§10.2).
        """
        story = self.get_object()

        if request.method == "GET":
            return Response(self._reaction_payload(story, request.user))

        if not request.user or not request.user.is_authenticated:
            return Response(
                {"detail": "Sign in to react to a story."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if request.method == "DELETE":
            StoryReaction.objects.filter(user=request.user, story=story).delete()
            return Response(self._reaction_payload(story, request.user))

        reaction_type = request.data.get("reaction_type")
        if reaction_type not in dict(StoryReaction.REACTION_CHOICES):
            return Response(
                {"detail": "Unknown reaction."}, status=status.HTTP_400_BAD_REQUEST
            )

        existing = StoryReaction.objects.filter(user=request.user, story=story).first()
        if existing and existing.reaction_type == reaction_type:
            existing.delete()
            return Response(self._reaction_payload(story, request.user))

        previous = existing.reaction_type if existing else None
        StoryReaction.objects.update_or_create(
            user=request.user, story=story, defaults={"reaction_type": reaction_type}
        )
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_REACTION_ADDED,
            user=request.user,
            story=story,
            visitor_id=AnalyticsEvent.SERVER_VISITOR_ID,
            metadata={"reaction": reaction_type, "previous": previous},
        )
        return Response(self._reaction_payload(story, request.user))

    @staticmethod
    def _reaction_payload(story, user):
        counts = dict(
            StoryReaction.objects.filter(story=story)
            .values_list("reaction_type")
            .annotate(total=Count("id"))
        )
        mine = None
        if user and user.is_authenticated:
            row = StoryReaction.objects.filter(user=user, story=story).first()
            mine = row.reaction_type if row else None

        return {
            # Every reaction type, including the ones at zero: a panel that
            # hid them would give a reader fewer ways to answer the longer a
            # story went unreacted-to.
            "reactions": [
                {"type": value, "label": label, "count": counts.get(value, 0)}
                for value, label in StoryReaction.REACTION_CHOICES
            ],
            "total": sum(counts.values()),
            "my_reaction": mine,
        }

    @action(detail=False, methods=["get"], url_path="facets")
    def facets(self, request):
        """Counts per filter value for the Library sidebar. Takes the list
        endpoint's filter params; each facet is counted under every *other*
        active filter, so its options keep showing what picking them would
        return. Cached 5 minutes per param set."""
        from .library_api import get_facets

        return Response(get_facets(dict(request.query_params.items()), nepali_only=wants_nepali_site(request)))

    @action(detail=False, methods=["get"], url_path="audiobook-stats", permission_classes=[AllowAny])
    def audiobook_stats(self, request):
        """Public summary for the Audiobooks hero: count of published stories
        with at least one audio file, sum of their audio durations in seconds,
        and how many have no known duration. Cached for 10 minutes."""
        cache_key = "story:audiobook-stats:v1"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        has_file_q = Q(audio_file__isnull=False) & ~Q(audio_file="")
        audio_stories = Story.objects.filter(published_story_q()).filter(
            Exists(Audio.objects.filter(has_file_q, story=OuterRef("pk")))
        )
        count = audio_stories.count()
        total_seconds = (
            Audio.objects.filter(has_file_q, story__in=audio_stories).aggregate(
                total=Coalesce(Sum("duration_seconds"), 0.0)
            )["total"]
            or 0.0
        )
        unknown_duration_count = audio_stories.exclude(
            Exists(Audio.objects.filter(has_file_q, story=OuterRef("pk"), duration_seconds__gt=0))
        ).count()
        data = {
            "count": count,
            "total_seconds": int(round(total_seconds)),
            "unknown_duration_count": unknown_duration_count,
        }
        cache.set(cache_key, data, 60 * 10)
        return Response(data)

    @action(
        detail=False,
        methods=["get"],
        url_path="quick-read-hero",
        permission_classes=[AllowAny],
    )
    def quick_read_hero(self, request):
        """Hero card for /quick-reads: for authenticated users, returns the most
        recently updated in-progress summary (0 < progress < 0.9) or a
        deterministic daily pick preferring unread summaries; for anonymous
        guests, returns today's deterministic pick (never continue)."""
        has_summary_q = Q(summary__isnull=False) & ~Q(summary__exact="")
        summary_stories = Story.objects.filter(published_story_q()).filter(has_summary_q)
        is_authed = bool(request.user and request.user.is_authenticated)

        if is_authed:
            in_progress = (
                QuickReadProgress.objects.filter(
                    user=request.user,
                    progress__gt=0,
                    progress__lt=0.9,
                    story__in=summary_stories,
                )
                .order_by("-updated_at")
                .first()
            )
            if in_progress is not None:
                story = (
                    Story.objects.published()
                    .for_card_list()
                    .filter(pk=in_progress.story_id)
                    .first()
                )
                if story is not None:
                    story._quick_read_progress = in_progress.progress
                    return Response(
                        {
                            "mode": "continue",
                            "story": StoryListSerializer(story, context={"request": request}).data,
                            "progress": in_progress.progress,
                        }
                    )

        ordered = summary_stories.order_by("id")
        if is_authed:
            read_ids = QuickReadProgress.objects.filter(
                user=request.user, progress__gte=0.9
            ).values_list("story_id", flat=True)
            unread = ordered.exclude(id__in=read_ids)
            pool = unread if unread.exists() else ordered
        else:
            pool = ordered

        count = pool.count()
        if count == 0:
            return Response({"mode": "none"})

        index = timezone.localdate().toordinal() % count
        pick_id = pool.values_list("id", flat=True)[index]
        story = Story.objects.published().for_card_list().filter(pk=pick_id).first()
        if story is None:
            return Response({"mode": "none"})
        return Response(
            {
                "mode": "pick",
                "story": StoryListSerializer(story, context={"request": request}).data,
            }
        )

    @action(detail=False, methods=["get"], url_path="collections")
    def collections(self, request):
        """Curated tiles for the Library's collections strip: each carries the
        filter params that reproduce it in the list, its size and three covers.
        Cached an hour."""
        from .library_api import get_collections

        return Response({"results": get_collections(request, nepali_only=wants_nepali_site(request))})

    @action(detail=False, methods=["get"], url_path="progress", permission_classes=[IsAuthenticated])
    def progress(self, request):
        """The signed-in reader's progress (0-1) for many stories in one request,
        so a card grid shows progress bars without a request per card."""
        from .library_api import story_progress

        raw = request.query_params.get("ids", "")
        ids = [int(token) for token in raw.split(",") if token.strip().isdigit()]
        return Response({"progress": {str(pk): value for pk, value in story_progress(request.user, ids).items()}})

    @action(detail=False, methods=["get"], url_path="surprise")
    def surprise(self, request):
        """One story, picked for this reader, at random from a good shortlist.

        "Random" only within a shortlist the existing recommendation path has
        already ranked (§8.1 asks to reuse it): a uniformly random story from
        the whole catalogue is not a surprise, it is a shrug. Signed-out readers
        get the popular-and-well-rated shortlist instead, which is the best
        available ranking with no taste signal to work from.

        `exclude` keeps the story the reader is looking at out of its own
        suggestion. `max_minutes` is §8.2's time filter, applied to the
        denormalized reading-time columns — the live estimate word-counts every
        chapter and cannot be filtered on.
        """
        exclude_slug = request.query_params.get("exclude") or ""
        try:
            max_minutes = int(request.query_params.get("max_minutes", ""))
        except (TypeError, ValueError):
            max_minutes = None

        candidates = None
        if request.user and request.user.is_authenticated:
            # Already excludes everything this reader has engaged with.
            ranked = list(recommend_stories_for(request.user, limit=SURPRISE_SHORTLIST))
            candidates = [story for story in ranked if story.slug != exclude_slug]

        if not candidates:
            queryset = Story.objects.published().for_card_list()
            if exclude_slug:
                queryset = queryset.exclude(slug=exclude_slug)
            if request.user and request.user.is_authenticated:
                # Nothing left after the personalized pass — fall back to the
                # whole catalogue rather than returning nothing, but still keep
                # finished stories out where any alternative exists (§8.1).
                completed_ids = StoryCompletion.objects.filter(
                    user=request.user
                ).values_list("story_id", flat=True)
                unread = queryset.exclude(id__in=completed_ids)
                queryset = unread if unread.exists() else queryset
            candidates = list(queryset.order_by("-rating", "-views", "-id")[:SURPRISE_SHORTLIST])

        if max_minutes:
            within_budget = [
                story
                for story in candidates
                if (story.cached_chapter_reading_minutes or story.cached_file_reading_minutes or 0)
                and (story.cached_chapter_reading_minutes or story.cached_file_reading_minutes)
                <= max_minutes
            ]
            # An empty result is the honest answer to "surprise me, in under
            # five minutes" when nothing that short exists — better than
            # quietly handing back something twice that long.
            candidates = within_budget

        if not candidates:
            return Response({"story": None})

        story = random.choice(candidates)
        return Response(
            {"story": StoryListSerializer(story, context={"request": request}).data}
        )

    @action(detail=True, methods=["post"], url_path="view")
    def register_view(self, request, slug=None):
        """Browser-fired view beacon. Counting here rather than in ``retrieve``
        keeps the visitor's own IP and User-Agent on the request, which is what
        makes the bot filter and the per-IP dedupe in ``_register_view`` mean
        anything — and it raises the bar for a "view" to "a real browser
        rendered this page", which no non-JS crawler clears."""
        story = self.get_object()
        self._register_view(request, story)
        return Response(status=status.HTTP_204_NO_CONTENT)

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

    @action(detail=True, methods=["get"], url_path="completion")
    def completion(self, request, slug=None):
        """Everything the end-of-story screen shows: one primary "read next"
        pick plus the themed sections around it.

        Composed from the same recommendation path as because-finished rather
        than a second one — see completion_recommendations. Open to anonymous
        readers, who get the generic similarity ranking instead of a
        personalized one; the completion screen has to work for a reader who
        finished a story without an account.

        `country_name` is resolved here because Story.country stores an ISO
        3166-1 alpha-2 code, and the section heading needs "More from Japan".
        """
        story = self.get_object()
        primary, sections = completion_recommendations(request.user, story)
        context = {"request": request}
        country_name = dict(COUNTRY_CHOICES).get(story.country) if story.country else None

        return Response(
            {
                "story_slug": story.slug,
                "story_title": story.title,
                "country": story.country or None,
                "country_name": country_name,
                "primary": (
                    StoryListSerializer(primary, context=context).data if primary else None
                ),
                "sections": [
                    {
                        "key": key,
                        "title": (
                            f"More from {country_name}"
                            if key == "more_from_country" and country_name
                            else title
                        ),
                        "stories": StoryListSerializer(
                            stories, many=True, context=context
                        ).data,
                    }
                    for key, title, stories in sections
                    if stories
                ],
            }
        )

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
        return _stream_audio_file_response(request, audio)


class BlogViewSet(ReadOnlyModelViewSet):
    """The public blog. List params: `topic` (slug), `q` (title + excerpt),
    `sort` = newest (default) | popular (unique readers, last 30 days) | oldest,
    `page` (12 per page), plus the older `linked_to_story=true|false` and
    `linked_story=<slug>` filters."""

    queryset = Blog.objects.all()
    lookup_field = "slug"
    serializer_class = BlogSerializer
    pagination_class = BlogPagination
    filter_backends = [SearchFilter]
    search_fields = ["title", "excerpt"]

    def get_serializer_class(self):
        return BlogListSerializer if self.action == "list" else BlogSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.action in ("list", "overview"):
            from .blog_api import blog_readers_30d

            context["reads_30d"] = blog_readers_30d()
        return context

    def get_queryset(self):
        params = self.request.query_params
        queryset = Blog.objects.published().prefetch_related(
            "topics",
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
        topic = params.get("topic")
        if topic:
            queryset = queryset.filter(topics__slug=topic).distinct()
        query = (params.get("q") or "").strip()
        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(excerpt__icontains=query))
        linked = params.get("linked_to_story")
        if linked == "true":
            queryset = queryset.filter(linked_stories__isnull=False).distinct()
        elif linked == "false":
            queryset = queryset.filter(linked_stories__isnull=True)
        linked_story_slug = params.get("linked_story")
        if linked_story_slug:
            queryset = queryset.filter(linked_stories__slug=linked_story_slug).distinct()

        sort = params.get("sort")
        if sort == "oldest":
            queryset = queryset.order_by("created_at")
        elif sort == "popular" and self.action == "list":
            from .blog_api import blog_readers_30d, popularity_order

            ids = popularity_order(list(queryset.values_list("pk", "created_at")), blog_readers_30d())
            if ids:
                position = Case(
                    *[When(pk=pk, then=index) for index, pk in enumerate(ids)],
                    default=len(ids),
                    output_field=IntegerField(),
                )
                queryset = queryset.order_by(position)
        return queryset

    @method_decorator(cache_page(60 * 15))
    @action(detail=False, methods=["get"], url_path="overview")
    def overview(self, request):
        """The top of the blog page: the featured post (the one pinned with
        `is_featured`, else the newest), the most-read posts of the last 30 days,
        each topic with its post count, and the books posts link to most.
        Cached 15 minutes."""
        from .blog_api import overview_parts

        parts = overview_parts()
        context = self.get_serializer_context()
        featured = parts["featured"]
        wanted = [*parts["most_read_ids"], *([featured.pk] if featured else [])]
        cards = {
            blog.pk: blog
            for blog in Blog.objects.published()
            .filter(pk__in=wanted)
            .prefetch_related(
                "topics",
                Prefetch(
                    "linked_stories",
                    queryset=Story.objects.published().select_related("author", "story_type"),
                    to_attr="published_linked_stories",
                ),
            )
        }

        def card(pk):
            return BlogListSerializer(cards[pk], context=context).data

        books = [
            {
                "id": story.pk,
                "title": story.title,
                "slug": story.slug,
                "author": story.author.name if story.author_id else None,
                "cover_image": get_cover_image_url(story.cover_image_file, story.cover_image, request, size=CARD_COVER_SIZE),
                "post_count": story.post_count,
            }
            for story in parts["linked_books"]
        ]
        return Response(
            {
                "featured": card(featured.pk) if featured else None,
                # True when an editor pinned it (is_featured); false when it is just the newest.
                "featured_is_pinned": bool(featured and featured.is_featured),
                "most_read": [card(pk) for pk in parts["most_read_ids"] if pk in cards],
                "topics": [
                    {"id": t.pk, "name": t.name, "slug": t.slug, "post_count": t.post_count} for t in parts["topics"]
                ],
                "total_posts": parts["total_posts"],
                "linked_books": books,
            }
        )


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
        if self.action == "list":
            # Subqueries, not joins: three joined Counts would multiply each other's rows.
            def count_of(model):
                return Coalesce(
                    Subquery(
                        model.objects.filter(story=OuterRef("pk")).order_by().values("story").annotate(n=Count("pk")).values("n"),
                        output_field=IntegerField(),
                    ),
                    0,
                )

            queryset = queryset.annotate(
                _chapter_count=count_of(Chapter), _audio_count=count_of(Audio), _video_count=count_of(Video)
            ).prefetch_related("genres", "categories", "tags", "themes")
        params = self.request.query_params

        is_published = params.get("is_published")
        if is_published in {"true", "false"}:
            queryset = queryset.filter(is_published=is_published == "true")

        is_completed = params.get("is_completed")
        if is_completed in {"true", "false"}:
            queryset = queryset.filter(is_completed=is_completed == "true")

        is_original = params.get("is_original")
        if is_original in {"true", "false"}:
            queryset = queryset.filter(is_original=is_original == "true")

        show_in_nepali_site = params.get("show_in_nepali_site")
        if show_in_nepali_site in {"true", "false"}:
            queryset = queryset.filter(show_in_nepali_site=show_in_nepali_site == "true")

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

        has_cover = params.get("has_cover")
        if has_cover in {"true", "false"}:
            # Same definition as the Analytics "no cover" gap: neither an uploaded file nor a URL.
            from .home_sections import missing_cover_q

            queryset = queryset.filter(missing_cover_q()) if has_cover == "false" else queryset.exclude(missing_cover_q())

        has_chapters = params.get("has_chapters")
        if has_chapters in {"true", "false"}:
            chapters = Exists(Chapter.objects.filter(story=OuterRef("pk")))
            queryset = queryset.filter(chapters) if has_chapters == "true" else queryset.filter(~chapters)

        # Same three states the list badges show: a story is Scheduled when it is
        # published but its publish_at is still in the future, Published once live, Draft otherwise.
        status_filter = params.get("status")
        if status_filter == "draft":
            queryset = queryset.filter(is_published=False)
        elif status_filter == "published":
            queryset = queryset.filter(published_story_q())
        elif status_filter == "scheduled":
            queryset = queryset.filter(is_published=True, publish_at__gt=timezone.now())

        sort = self.ADMIN_SORTS.get(params.get("sort", ""))
        if sort:
            queryset = queryset.order_by(*sort)

        return queryset

    def paginate_queryset(self, queryset):
        page = super().paginate_queryset(queryset)
        if page is not None and self.action == "list":
            groups = {story.translation_group for story in page if story.translation_group}
            by_group = {}
            for sibling in Story.objects.filter(translation_group__in=groups).only(
                "id", "slug", "language", "title", "translation_group"
            ).order_by("language"):
                by_group.setdefault(sibling.translation_group, []).append(sibling)
            self._translations_by_group = by_group
        return page

    def get_serializer_context(self):
        context = super().get_serializer_context()
        batched = getattr(self, "_translations_by_group", None)
        if batched is not None:
            context["translations_by_group"] = batched
        return context

    # Whitelisted ?sort= values; anything else keeps the default (newest first).
    ADMIN_SORTS = {
        "newest": ("-id",),
        "oldest": ("id",),
        "title_asc": ("title", "id"),
        "title_desc": ("-title", "-id"),
    }

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """Counts for the admin Stories overview, in one pass over the table.

        published / draft / scheduled follow the list badges: Scheduled is
        is_published with a future publish_at, Published is live now (the same
        published_story_q the public site uses), Draft is not published. The
        missing_* counts are among Published stories only."""
        from .home_sections import missing_cover_q

        live = published_story_q()
        no_summary = Q(summary__isnull=True) | Q(summary__exact="")
        no_retro = Q(retrospective__isnull=True) | Q(retrospective__exact="")
        counts = (
            Story.objects.annotate(_has_chapters=Exists(Chapter.objects.filter(story=OuterRef("pk"))))
            .aggregate(
                total=Count("id"),
                published=Count("id", filter=live),
                draft=Count("id", filter=Q(is_published=False)),
                scheduled=Count("id", filter=Q(is_published=True, publish_at__gt=timezone.now())),
                missing_summary=Count("id", filter=live & no_summary),
                missing_retrospective=Count("id", filter=live & no_retro),
                missing_cover=Count("id", filter=live & missing_cover_q()),
                missing_chapters=Count("id", filter=live & Q(_has_chapters=False)),
            )
        )
        return Response(counts)

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

    def _featured_rows(self, request):
        """The admin lineup: featured stories and videos together, by rank."""
        context = {"request": request}
        stories = Story.objects.filter(featured_rank__isnull=False).select_related("author")
        videos = Video.objects.filter(featured_rank__isnull=False).select_related("story", "video_type")
        rows = [{**FeaturedStoryAdminSerializer(s, context=context).data, "kind": "story"} for s in stories]
        rows += [dict(FeaturedVideoAdminSerializer(v, context=context).data) for v in videos]
        return sorted(rows, key=lambda row: row["featured_rank"])

    @action(detail=False, methods=["get", "put"], url_path="featured")
    def featured(self, request):
        """The homepage's manually-curated Featured lineup — an ordered,
        at-most-5 list of stories and/or videos. PUT replaces the whole set
        atomically (same validate-then-replace shape as
        AudioAdminViewSet.transcript_cues): a bad payload leaves the existing
        set untouched. Send `items: [{kind: "story"|"video", id}]`; the older
        `story_ids: [...]` form still works for stories only. HomeDataAPIView
        reads the ranks directly and backfills when the lineup is empty."""
        if request.method == "GET":
            return Response(self._featured_rows(request))

        if "items" in request.data:
            raw_items = request.data.get("items")
            valid_shape = isinstance(raw_items, list) and all(
                isinstance(i, dict) and i.get("kind") in ("story", "video") and isinstance(i.get("id"), int)
                for i in raw_items
            )
            if not valid_shape:
                return Response(
                    {"detail": 'items must be a list of {"kind": "story"|"video", "id": number}.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            items = [(i["kind"], i["id"]) for i in raw_items]
        else:
            story_ids = request.data.get("story_ids", [])
            if not isinstance(story_ids, list) or not all(isinstance(i, int) for i in story_ids):
                return Response(
                    {"detail": "story_ids must be a list of story IDs."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            items = [("story", i) for i in story_ids]

        if len(items) > 5:
            return Response(
                {"detail": "You can feature at most 5 items."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(items) != len(set(items)):
            return Response(
                {"detail": "The same item can't be featured twice."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        for kind, model in (("story", Story), ("video", Video)):
            wanted = [i for k, i in items if k == kind]
            found = set(model.objects.filter(id__in=wanted).values_list("id", flat=True))
            missing = [i for i in wanted if i not in found]
            if missing:
                return Response(
                    {"detail": f"{kind.capitalize()} id(s) not found: {missing}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            Story.objects.filter(featured_rank__isnull=False).update(featured_rank=None)
            Video.objects.filter(featured_rank__isnull=False).update(featured_rank=None)
            for index, (kind, pk) in enumerate(items, start=1):
                (Story if kind == "story" else Video).objects.filter(pk=pk).update(featured_rank=index)

        return Response(self._featured_rows(request))

    @action(detail=False, methods=["get", "put", "delete"], url_path="daily")
    def daily(self, request):
        """Read or replace the Daily Story for one explicit UTC date."""
        raw_date = request.query_params.get("date") or request.data.get("date")
        try:
            selected_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
        except (TypeError, ValueError):
            return Response({"detail": "date must use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        current = (
            DailyStory.objects.select_related("story", "story__author", "video", "video__story", "video__video_type")
            .filter(date=selected_date)
            .first()
        )
        if request.method == "GET":
            if not current:
                return Response(None)
            return Response(DailyStoryAdminSerializer(current, context={"request": request}).data)

        if request.method == "DELETE":
            if current:
                current.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        payload = request.data.copy()
        payload["date"] = selected_date.isoformat()
        serializer = DailyStoryAdminSerializer(current, data=payload)
        serializer.is_valid(raise_exception=True)
        daily_story = serializer.save()
        return Response(
            DailyStoryAdminSerializer(daily_story, context={"request": request}).data,
            status=status.HTTP_200_OK if current else status.HTTP_201_CREATED,
        )

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
    queryset = (
        Blog.objects.prefetch_related(
            Prefetch("linked_stories", queryset=Story.objects.only("id", "title", "slug")),
            Prefetch("linked_blogs", queryset=Blog.objects.only("id", "title", "slug")),
            "topics",
        )
        .all()
        .order_by("-created_at", "-id")
    )
    serializer_class = BlogAdminSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsSuperUser]
    pagination_class = BlogPagination
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "excerpt"]

    # Whitelisted ?sort= values; anything else falls back to default (newest first).
    ADMIN_SORTS = {
        "newest": ("-created_at", "-id"),
        "oldest": ("created_at", "id"),
        "updated": ("-updated_at", "-id"),
        "title_asc": ("title", "id"),
        "title_desc": ("-title", "-id"),
    }

    @staticmethod
    def _missing_excerpt_q():
        return Q(excerpt__isnull=True) | Q(excerpt="") | Q(excerpt__regex=r"^\s*$")

    @staticmethod
    def _missing_cover_q():
        return Q(cover_image_file__isnull=True) | Q(cover_image_file="")

    def get_serializer_class(self):
        if self.action == "list":
            return BlogAdminListSerializer
        return BlogAdminSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "list":
            queryset = queryset.defer("content")

        params = self.request.query_params

        status_filter = params.get("status")
        if status_filter == "published":
            queryset = queryset.filter(published_blog_q())
        elif status_filter == "scheduled":
            queryset = queryset.filter(is_published=True, publish_at__gt=timezone.now())
        elif status_filter == "draft":
            queryset = queryset.filter(is_published=False)

        has_excerpt = params.get("has_excerpt")
        if has_excerpt in {"true", "false"}:
            no_excerpt = self._missing_excerpt_q()
            queryset = queryset.filter(no_excerpt) if has_excerpt == "false" else queryset.exclude(no_excerpt)

        has_topics = params.get("has_topics")
        if has_topics in {"true", "false"}:
            topics_exists = Exists(Blog.topics.through.objects.filter(blog_id=OuterRef("pk")))
            queryset = queryset.filter(topics_exists) if has_topics == "true" else queryset.filter(~topics_exists)

        has_cover = params.get("has_cover")
        if has_cover in {"true", "false"}:
            no_cover = self._missing_cover_q()
            queryset = queryset.filter(no_cover) if has_cover == "false" else queryset.exclude(no_cover)

        has_linked_stories = params.get("has_linked_stories")
        if has_linked_stories in {"true", "false"}:
            linked_stories_exists = Exists(Blog.linked_stories.through.objects.filter(blog_id=OuterRef("pk")))
            queryset = (
                queryset.filter(linked_stories_exists)
                if has_linked_stories == "true"
                else queryset.filter(~linked_stories_exists)
            )

        topic_id = params.get("topic")
        if topic_id and topic_id.isdigit():
            queryset = queryset.filter(
                Exists(Blog.topics.through.objects.filter(blog_id=OuterRef("pk"), blogtopic_id=int(topic_id)))
            )

        sort = self.ADMIN_SORTS.get(params.get("sort", ""), self.ADMIN_SORTS["newest"])
        queryset = queryset.order_by(*sort)

        return queryset

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """Counts for the admin Blog overview, in a single aggregate pass."""
        counts = Blog.objects.annotate(
            _has_topics=Exists(Blog.topics.through.objects.filter(blog_id=OuterRef("pk"))),
            _has_linked_stories=Exists(Blog.linked_stories.through.objects.filter(blog_id=OuterRef("pk"))),
        ).aggregate(
            total=Count("id"),
            published=Count("id", filter=published_blog_q()),
            draft=Count("id", filter=Q(is_published=False)),
            scheduled=Count("id", filter=Q(is_published=True, publish_at__gt=timezone.now())),
            missing_excerpt=Count("id", filter=self._missing_excerpt_q()),
            missing_topics=Count("id", filter=Q(_has_topics=False)),
            missing_cover=Count("id", filter=self._missing_cover_q()),
            no_linked_stories=Count("id", filter=Q(_has_linked_stories=False)),
        )
        return Response(counts)

    @action(detail=True, methods=["post"], url_path="generate-excerpt")
    def generate_excerpt(self, request, pk=None):
        blog = self.get_object()
        Blog.objects.filter(pk=blog.pk).update(excerpt_status=Blog.GEN_STATUS_PENDING)
        blog.refresh_from_db()
        transaction.on_commit(lambda: ai_generation_executor.submit(run_generate_blog_excerpt, blog.id))
        return Response(self.get_serializer(blog).data, status=status.HTTP_202_ACCEPTED)


    @action(detail=True, methods=["post"], url_path="suggest-topics")
    def suggest_topics(self, request, pk=None):
        """Asks the model for 1-2 topics (from the seeded list) and applies them
        when it answers. Runs in the background like generate-excerpt; the post's
        topics_status says where it is."""
        blog = self.get_object()
        Blog.objects.filter(pk=blog.pk).update(topics_status=Blog.GEN_STATUS_PENDING)
        blog.refresh_from_db()
        transaction.on_commit(lambda: ai_generation_executor.submit(run_suggest_blog_topics, blog.id))
        return Response(self.get_serializer(blog).data, status=status.HTTP_202_ACCEPTED)


class BlogTopicAdminViewSet(ModelViewSet):
    """The topic vocabulary: list, create, rename, reorder, delete."""

    serializer_class = BlogTopicSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None

    def get_queryset(self):
        return BlogTopic.objects.annotate(post_count=Count("blogs", distinct=True)).order_by("order", "name")


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


def _title_from_audio_filename(filename: str) -> str:
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    cleaned = re.sub(r"[_\-]+", " ", base)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "Untitled Track"
    return cleaned.title()


class AudiobookSeriesAdminViewSet(ModelViewSet):
    serializer_class = AudiobookSeriesAdminSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name", "slug", "description"]

    def get_queryset(self):
        return (
            AudiobookSeries.objects.annotate(
                _audiobook_count=Count("audiobooks", distinct=True)
            )
            .order_by("order", "name", "id")
        )


class AudiobookAdminViewSet(ModelViewSet):
    permission_classes = [IsSuperUser]
    pagination_class = CataloguePagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_serializer_class(self):
        if self.action == "list":
            return AudiobookAdminListSerializer
        return AudiobookAdminDetailSerializer

    def get_queryset(self):
        track_with_file = Q(tracks__audio_file__isnull=False) & ~Q(tracks__audio_file="")
        qs = (
            Audiobook.objects.select_related("story", "story__author", "author", "series")
            .prefetch_related("genres", "story__genres")
            .annotate(
                _track_count=Count("tracks", filter=track_with_file, distinct=True),
                _total_duration_seconds=Sum(
                    "tracks__duration_seconds",
                    filter=track_with_file & Q(tracks__duration_seconds__gt=0),
                ),
                _missing_duration_count=Count(
                    "tracks",
                    filter=track_with_file
                    & (Q(tracks__duration_seconds__isnull=True) | Q(tracks__duration_seconds__lte=0)),
                    distinct=True,
                ),
            )
        )
        if self.action != "list":
            qs = qs.prefetch_related(
                Prefetch(
                    "tracks",
                    queryset=Audio.objects.annotate(
                        _has_timed_cues=Exists(
                            AudioTranscriptCue.objects.filter(audio=OuterRef("pk"))
                        ),
                        _cue_count=Count("transcript_cues", distinct=True),
                    ).order_by("order", "id"),
                )
            )

        if self.action == "list":
            params = self.request.query_params
            q = (params.get("q") or "").strip()
            if q:
                qs = qs.filter(
                    Q(title__icontains=q)
                    | Q(display_title__icontains=q)
                    | Q(slug__icontains=q)
                    | Q(narrator__icontains=q)
                    | Q(author__name__icontains=q)
                    | Q(story__title__icontains=q)
                    | Q(story__author__name__icontains=q)
                )

            story_link = (params.get("story_link") or "").strip().lower()
            if story_link == "linked":
                qs = qs.filter(story__isnull=False)
            elif story_link == "standalone":
                qs = qs.filter(story__isnull=True)

            series_param = (params.get("series") or "").strip()
            if series_param and series_param.isdigit():
                qs = qs.filter(series_id=int(series_param))

            status_param = (params.get("status") or "").strip().lower()
            now = timezone.now()
            if status_param == "draft":
                qs = qs.filter(is_published=False)
            elif status_param == "scheduled":
                qs = qs.filter(is_published=True, publish_at__gt=now)
            elif status_param == "published":
                qs = qs.filter(is_published=True).filter(
                    Q(publish_at__isnull=True) | Q(publish_at__lte=now)
                )

            ordering = (params.get("ordering") or "-updated_at").strip()
            if ordering == "title":
                qs = qs.order_by(Lower("title"), "id")
            elif ordering == "-title":
                qs = qs.order_by(Lower("title").desc(), "-id")
            elif ordering == "duration":
                qs = qs.order_by(F("_total_duration_seconds").asc(nulls_last=True), "id")
            elif ordering == "-duration":
                qs = qs.order_by(F("_total_duration_seconds").desc(nulls_last=True), "-id")
            elif ordering == "tracks":
                qs = qs.order_by("_track_count", "id")
            elif ordering == "-tracks":
                qs = qs.order_by("-_track_count", "-id")
            else:
                qs = qs.order_by("-updated_at", "-id")

        return qs

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        missing_duration = (
            Audio.objects.filter(audiobook__isnull=False, audio_file__isnull=False)
            .exclude(audio_file="")
            .filter(Q(duration_seconds__isnull=True) | Q(duration_seconds__lte=0))
            .count()
        )
        return Response(
            {
                "total": Audiobook.objects.count(),
                "standalone": Audiobook.objects.filter(story__isnull=True).count(),
                "series_count": AudiobookSeries.objects.count(),
                "missing_duration": missing_duration,
            }
        )

    @action(detail=False, methods=["get"], url_path="story-search")
    def story_search(self, request):
        q = (request.query_params.get("q") or "").strip()
        stories = (
            Story.objects.select_related("author")
            .annotate(_audiobook_count=Count("audiobooks", distinct=True))
            .order_by("title", "id")
        )
        if q:
            stories = stories.filter(
                Q(title__icontains=q) | Q(author__name__icontains=q) | Q(slug__icontains=q)
            )
        results = []
        for story in stories[:10]:
            results.append(
                {
                    "id": story.id,
                    "title": story.title,
                    "slug": story.slug,
                    "author": story.author.name if story.author_id else "",
                    "cover": get_cover_image_url(
                        story.cover_image_file, story.cover_image, request, size=CARD_COVER_SIZE
                    ),
                    "audiobook_count": story._audiobook_count,
                }
            )
        return Response(results)

    def _next_track_order(self, audiobook: Audiobook) -> int:
        max_ab = audiobook.tracks.aggregate(m=Max("order")).get("m") or 0
        max_story = 0
        if audiobook.story_id is not None:
            max_story = Audio.objects.filter(story_id=audiobook.story_id).aggregate(m=Max("order")).get("m") or 0
        return max(max_ab, max_story) + 1

    def _close_track_order_gap(self, audiobook: Audiobook, deleted_order: int):
        later_tracks = list(
            Audio.objects.filter(audiobook=audiobook, order__gt=deleted_order).order_by("order", "id")
        )
        for track in later_tracks:
            track.order = max(1, track.order - 1)
            track.save(update_fields=["order"])

    @action(detail=True, methods=["post"], url_path="tracks")
    def upload_tracks(self, request, pk=None):
        from . import reading_time
        from .audio_processing import normalize_uploaded_audio
        from .models import MAX_AUDIO_UPLOAD_SIZE

        audiobook = self.get_object()
        files = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not files:
            return Response(
                {"detail": "No audio files provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = []
        errors = []
        next_order = self._next_track_order(audiobook)

        for uploaded in files:
            filename = getattr(uploaded, "name", "") or "track.mp3"
            ext = os.path.splitext(filename)[1].lower()
            if ext != ".mp3":
                errors.append({"filename": filename, "error": "Only .mp3 files are supported."})
                continue
            file_size = getattr(uploaded, "size", 0) or 0
            if file_size > MAX_AUDIO_UPLOAD_SIZE:
                errors.append({"filename": filename, "error": "File exceeds maximum allowed size."})
                continue
            try:
                normalized = normalize_uploaded_audio(uploaded)
                raw_bytes = reading_time.read_local_upload_bytes(normalized)
                duration = reading_time.probe_audio_duration_from_bytes(raw_bytes)
                size_bytes = len(raw_bytes) if raw_bytes else (getattr(normalized, "size", 0) or 0)
            except Exception as exc:
                errors.append({"filename": filename, "error": str(exc)})
                continue

            title = _title_from_audio_filename(filename)
            base_slug = slugify(title)[:240] or f"track-{next_order}"
            track = Audio.objects.create(
                audiobook=audiobook,
                story=audiobook.story,
                title=title,
                slug=f"{base_slug}-{next_order}"[:256],
                audio_file=normalized,
                order=next_order,
                duration_seconds=duration,
                file_size_bytes=size_bytes,
            )
            created.append(track)
            next_order += 1

        audiobook.refresh_cache()
        payload = {
            "created": AudiobookTrackAdminSerializer(
                created, many=True, context=self.get_serializer_context()
            ).data,
            "errors": errors,
        }
        if not created:
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        if errors:
            return Response(payload, status=status.HTTP_207_MULTI_STATUS)
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="tracks/reorder")
    def reorder_tracks(self, request, pk=None):
        audiobook = self.get_object()
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(x, int) for x in ids):
            return Response(
                {"ids": "Provide a list of track IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_ids = list(audiobook.tracks.values_list("id", flat=True))
        if len(ids) != len(set(ids)) or set(ids) != set(existing_ids):
            return Response(
                {"ids": "Track IDs must match the exact tracks belonging to this audiobook."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # Shift orders out of range first so neither (audiobook, order) nor
            # (story, order) unique constraints collide mid-swap.
            offset = 100000
            Audio.objects.filter(audiobook=audiobook).update(order=F("order") + offset)
            for idx, track_id in enumerate(ids, start=1):
                Audio.objects.filter(pk=track_id, audiobook=audiobook).update(order=idx)

        audiobook.refresh_cache()
        ordered = (
            audiobook.tracks.annotate(
                _has_timed_cues=Exists(AudioTranscriptCue.objects.filter(audio=OuterRef("pk"))),
                _cue_count=Count("transcript_cues", distinct=True),
            )
            .order_by("order", "id")
        )
        return Response(
            AudiobookTrackAdminSerializer(
                ordered, many=True, context=self.get_serializer_context()
            ).data
        )

    @action(detail=True, methods=["patch", "delete"], url_path=r"tracks/(?P<track_id>\d+)")
    def track_detail(self, request, pk=None, track_id=None):
        audiobook = self.get_object()
        track = get_object_or_404(Audio, pk=track_id, audiobook=audiobook)

        if request.method == "DELETE":
            deleted_order = track.order
            with transaction.atomic():
                track.delete()
                self._close_track_order_gap(audiobook, deleted_order)
            audiobook.refresh_cache()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = AudiobookTrackAdminSerializer(
            track, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        audiobook.refresh_cache()
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"tracks/(?P<track_id>\d+)/reread-duration",
    )
    def reread_track_duration(self, request, pk=None, track_id=None):
        from . import reading_time

        audiobook = self.get_object()
        track = get_object_or_404(Audio, pk=track_id, audiobook=audiobook)
        duration = reading_time.probe_audio_duration_seconds(track.audio_file)
        update_fields = ["duration_seconds"]
        track.duration_seconds = duration
        try:
            if track.audio_file and track.audio_file.size:
                track.file_size_bytes = track.audio_file.size
                update_fields.append("file_size_bytes")
        except Exception:
            pass
        track.save(update_fields=update_fields)
        audiobook.refresh_cache()
        return Response(
            AudiobookTrackAdminSerializer(track, context=self.get_serializer_context()).data
        )

    @action(
        detail=True,
        methods=["post"],
        url_path=r"tracks/(?P<track_id>\d+)/move",
    )
    def move_track(self, request, pk=None, track_id=None):
        source = self.get_object()
        track = get_object_or_404(Audio, pk=track_id, audiobook=source)
        target_id = request.data.get("audiobook_id")
        if not target_id:
            return Response(
                {"audiobook_id": "Target audiobook is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = get_object_or_404(Audiobook, pk=target_id)
        if target.pk == source.pk:
            return Response(
                {"audiobook_id": "Choose a different audiobook."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_order = track.order
        with transaction.atomic():
            next_order = self._next_track_order(target)
            track.audiobook = target
            track.story = target.story
            track.order = next_order
            track.save(update_fields=["audiobook", "story", "order"])
            self._close_track_order_gap(source, old_order)

        source.refresh_cache()
        target.refresh_cache()
        return Response(
            AudiobookTrackAdminSerializer(track, context=self.get_serializer_context()).data
        )


class VideoAdminViewSet(ModelViewSet):
    """Serves two callers: the story editor's per-story video list
    (`?story=<id>`, unpaginated, in playback order) and the standalone
    Videos admin page (paged, newest first, optionally `?has_story=`)."""

    queryset = Video.objects.select_related("story", "video_type", "series").all().order_by("-created_at", "-id")
    serializer_class = VideoAdminSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "youtube_id", "story__title"]

    @property
    def paginator(self):
        if not hasattr(self, "_paginator"):
            if self.request.query_params.get("story"):
                self._paginator = AdminStoryItemPagination()
            else:
                self._paginator = PageNumberPagination()
        return self._paginator

    def get_queryset(self):
        queryset = super().get_queryset()
        story_id = self.request.query_params.get("story")
        if story_id:
            queryset = queryset.filter(story_id=story_id).order_by("order", "id")
        has_story = (self.request.query_params.get("has_story") or "").lower()
        if has_story == "true":
            queryset = queryset.filter(story__isnull=False)
        elif has_story == "false":
            queryset = queryset.filter(story__isnull=True)
        series = self.request.query_params.get("series")
        if series:
            queryset = queryset.filter(series__slug=series)
        return queryset

    def perform_destroy(self, instance):
        story = instance.story
        deleted_order = instance.order
        instance.delete()

        # Keep order contiguous from 1 — see ChapterAdminViewSet.perform_destroy.
        # Standalone videos have no sibling ordering to maintain.
        if story is None:
            return
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
                .for_card_list()
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

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.query_params.get("missing_portrait") == "1":
            queryset = queryset.filter(Q(image__isnull=True) | Q(image=""))
        return queryset

    def perform_create(self, serializer):
        name = serializer.validated_data["name"]
        base = slugify(name) or "author"
        slug = base
        suffix = 2
        while Author.objects.filter(slug=slug).exists():
            slug = f"{base}-{suffix}"
            suffix += 1
        serializer.save(slug=slug)

    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        source = self.get_object()
        target_id = request.data.get("target")
        target = get_object_or_404(Author, pk=target_id)
        if source.pk == target.pk:
            raise ValidationError({"target": "Choose a different target author."})
        moved = merge_authors(source, target)
        return Response({"target": AdminAuthorSerializer(target, context=self.get_serializer_context()).data, "moved": [{"relation": label, "count": count} for label, count in moved]})

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


class HeroTemplateAdminViewSet(ModelViewSet):
    """Homepage hero templates for the admin panel.

    Which template the site shows is decided by HeroTemplate.objects.current():
    a scheduled one inside its window, otherwise the default. The public
    homepage response is cached for DISCOVERY_CACHE_SECONDS, so a change shows
    up within that long rather than instantly.
    """

    queryset = HeroTemplate.objects.all()
    serializer_class = AdminHeroTemplateSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None

    def get_serializer_context(self):
        context = super().get_serializer_context()
        now = timezone.now()
        current = HeroTemplate.objects.current(now)
        context.update({"now": now, "current_id": current.pk if current else None})
        return context

    @action(detail=True, methods=["post"], url_path="set-default")
    def set_default(self, request, pk=None):
        template = self.get_object()
        with transaction.atomic():
            HeroTemplate.objects.filter(is_default=True).exclude(pk=template.pk).update(is_default=False)
            template.is_default = True
            template.save(update_fields=["is_default", "updated_at"])
        return Response(self.get_serializer(template).data)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """A copy to start a variant from — never the default and never
        scheduled, so the copy can't change what the site shows until an
        editor says so."""
        source = self.get_object()
        copy = HeroTemplate.objects.get(pk=source.pk)
        copy.pk = None
        copy.name = f"{source.name} (copy)"[:80]
        copy.is_default = False
        copy.starts_at = None
        copy.ends_at = None
        copy.save()
        return Response(self.get_serializer(copy).data, status=status.HTTP_201_CREATED)


class HomeSectionAdminViewSet(ModelViewSet):
    """Admin management of the homepage body sections: CRUD, enable/disable,
    drag-to-reorder and duplication. The public homepage response is cached for
    DISCOVERY_CACHE_SECONDS, so changes show up within that long."""

    queryset = HomeSection.objects.all().order_by("position", "id")
    serializer_class = HomeSectionAdminSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None

    def perform_create(self, serializer):
        if "position" not in serializer.validated_data:
            last = HomeSection.objects.aggregate(m=Max("position"))["m"]
            serializer.save(position=0 if last is None else last + 1)
        else:
            serializer.save()

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        ids = request.data.get("ids")
        existing = set(HomeSection.objects.values_list("pk", flat=True))
        if not isinstance(ids, list) or set(ids) != existing or len(ids) != len(existing):
            return Response(
                {"detail": "Send every section id exactly once, in the new order."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            for position, pk in enumerate(ids):
                HomeSection.objects.filter(pk=pk).update(position=position)
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        section = self.get_object()
        section.is_enabled = not section.is_enabled
        section.save(update_fields=["is_enabled", "updated_at"])
        return Response(self.get_serializer(section).data)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """A disabled copy right below the original, so it can't change the
        live page until an editor turns it on."""
        source = self.get_object()
        with transaction.atomic():
            HomeSection.objects.filter(position__gt=source.position).update(position=F("position") + 1)
            copy = HomeSection.objects.get(pk=source.pk)
            copy.pk = None
            copy.title = f"{source.title} (copy)"[:120] if source.title else ""
            copy.is_enabled = False
            copy.anchor_id = ""
            copy.position = source.position + 1
            copy.save()
        return Response(self.get_serializer(copy).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """Clicks and click-through per section for the last 7 and 30 days."""
        from .home_metrics import section_click_stats

        return Response(section_click_stats())

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Resolve unsaved content-section values without writing anything."""
        section_type = request.data.get("section_type")
        if section_type not in HomeSection.CONTENT_TYPES:
            return Response(
                {"detail": "Only story, video, and blog sections can be previewed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer_data = {"title": "Preview", "section_type": section_type}
        for field in ("variant", "item_limit", "allow_repeats", "config", "display"):
            if field in request.data:
                serializer_data[field] = request.data[field]
        serializer = self.get_serializer(data=serializer_data)
        if not serializer.is_valid():
            def first_error(value):
                if isinstance(value, dict):
                    preferred = value.get("config")
                    return first_error(preferred if preferred is not None else next(iter(value.values())))
                if isinstance(value, (list, tuple)):
                    return first_error(value[0]) if value else "Invalid preview settings."
                return str(value)

            return Response(
                {"detail": first_error(serializer.errors)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        section = HomeSection(**serializer.validated_data)
        preview_limit = min(section.item_limit, 6)
        context = {"request": request}
        from .home_sections import blog_queryset, clean_display, story_queryset, video_queryset

        display = clean_display(section.display)
        if section_type in (HomeSection.TYPE_STORIES_QUERY, HomeSection.TYPE_STORIES_MANUAL):
            items, total = story_queryset(section, with_total=True)
            story_ser = StoryCardWithSummarySerializer if display.get("show_summary") else StoryListSerializer
            data = story_ser(items[:preview_limit], many=True, context=context).data
        elif section_type == HomeSection.TYPE_VIDEOS:
            items, total = video_queryset(section, with_total=True)
            data = PublicVideoSerializer(items[:preview_limit], many=True, context=context).data
        else:
            items, total = blog_queryset(section, with_total=True)
            data = BlogCardSerializer(items[:preview_limit], many=True, context=context).data
        return Response({"total_matching": total, "items": data})

    @action(detail=False, methods=["get"], url_path="missing-covers")
    def missing_covers(self, request):
        """Published stories with neither cover image nor cover URL — they
        render the placeholder tile, and are skipped by sections that turn on
        "only stories with a cover"."""
        from .home_sections import missing_cover_q

        stories = Story.objects.published().filter(missing_cover_q()).order_by("-views", "title")
        rows = [
            {"id": s.pk, "title": s.title, "slug": s.slug, "views": s.views}
            for s in stories.only("id", "title", "slug", "views")
        ]
        return Response({"total": len(rows), "results": rows})

    @action(detail=False, methods=["get"])
    def types(self, request):
        """Static description of the section types and choice lists the admin
        form is built from."""
        from .home_sections import (
            AD_SIZES,
            BANNER_THEMES,
            BLOG_SORT_OPTIONS,
            CARD_STYLE_OPTIONS,
            LAYOUT_OPTIONS,
            STORY_SORT_OPTIONS,
            TYPE_META,
            VIDEO_SORT_OPTIONS,
        )
        from .models import HOME_SECTION_ICON_NAMES

        return Response(
            {
                "types": [
                    {
                        "value": value,
                        "label": label,
                        **TYPE_META[value],
                        "layouts": [LAYOUT_OPTIONS[code] for code in TYPE_META[value].get("layouts", [])],
                        "card_styles": [CARD_STYLE_OPTIONS[code] for code in TYPE_META[value].get("card_styles", [])],
                    }
                    for value, label in HomeSection.TYPE_CHOICES
                ],
                "variants": [{"value": v, "label": l} for v, l in HomeSection.VARIANT_CHOICES],
                "layouts": [LAYOUT_OPTIONS[v] for v, _l in HomeSection.LAYOUT_CHOICES],
                "card_styles": [CARD_STYLE_OPTIONS[v] for v, _l in HomeSection.CARD_STYLE_CHOICES],
                "show_to": [{"value": v, "label": l} for v, l in HomeSection.SHOW_TO_CHOICES],
                "sorts": {
                    "stories": STORY_SORT_OPTIONS,
                    "videos": VIDEO_SORT_OPTIONS,
                    "blogs": BLOG_SORT_OPTIONS,
                },
                "banner_themes": BANNER_THEMES,
                "ad_sizes": AD_SIZES,
                "icons": HOME_SECTION_ICON_NAMES,
                "devices": [{"value": v, "label": l} for v, l in HomeSection.DEVICE_CHOICES],
            }
        )

    @action(detail=False, methods=["get"])
    def templates(self, request):
        from .home_section_templates import HOME_SECTION_TEMPLATES

        return Response(
            [
                {
                    "key": t["key"],
                    "name": t["name"],
                    "description": t["description"],
                    "section_type": t["section_type"],
                    "layout": t["layout"],
                    "card_style": t["card_style"],
                    "summary": t["summary"],
                }
                for t in HOME_SECTION_TEMPLATES
            ]
        )

    @action(detail=False, methods=["post"], url_path="from-template")
    def from_template(self, request):
        from .home_section_templates import TEMPLATES_BY_KEY

        key = (request.data.get("key") or "").strip()
        template = TEMPLATES_BY_KEY.get(key)
        if not template:
            return Response({"detail": "Template not found."}, status=status.HTTP_404_NOT_FOUND)
        payload = {
            k: template[k]
            for k in (
                "section_type",
                "variant",
                "title",
                "subtitle",
                "icon",
                "layout",
                "card_style",
                "columns_mobile",
                "columns_desktop",
                "item_limit",
                "allow_repeats",
                "show_see_all",
                "config",
                "display",
                "device",
            )
        }
        payload["is_enabled"] = False
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        last = HomeSection.objects.aggregate(m=Max("position"))["m"]
        section = serializer.save(position=0 if last is None else last + 1)
        return Response(self.get_serializer(section).data, status=status.HTTP_201_CREATED)


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


class VideoTypeAdminViewSet(ModelViewSet):
    """Video-type management for the admin panel. Videos keep existing when a
    type is deleted (their video_type is just cleared)."""

    queryset = VideoType.objects.all().order_by("name")
    serializer_class = AdminVideoTypeSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]


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


def _unique_journey_slug(title: str) -> str:
    base_slug = slugify(title) or "journey"
    slug = base_slug
    index = 2
    while StoryJourney.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


class StoryJourneyAdminViewSet(ModelViewSet):
    """Journey management for the admin panel.

    Exists because Django admin is unusable on this deployment (Django 5.0 on
    Python 3.14 — `BaseContext.__copy__` breaks every admin change list), and
    because every other kind of content here is managed from the custom panel
    anyway. The Django admin registration stays as a fallback for whenever that
    version mismatch is resolved.
    """

    queryset = StoryJourney.objects.all().prefetch_related("items__story").order_by("order", "title")
    serializer_class = AdminStoryJourneySerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["title", "description"]

    def perform_create(self, serializer):
        serializer.save(slug=_unique_journey_slug(serializer.validated_data["title"]))

    @action(detail=True, methods=["get", "put"], url_path="items")
    def items(self, request, pk=None):
        """Read or replace a journey's ordered story list.

        PUT replaces the whole set, which matches how the editor works — they
        arrange the journey and save it, rather than issuing add/remove/reorder
        operations one at a time. Position is taken from the order the client
        sends, so reordering needs no separate endpoint.
        """
        journey = self.get_object()

        if request.method == "GET":
            return Response(
                AdminStoryJourneyItemSerializer(
                    journey.items.select_related("story").all(),
                    many=True,
                    context={"request": request},
                ).data
            )

        payload = request.data.get("items")
        if not isinstance(payload, list):
            return Response(
                {"detail": "items must be a list."}, status=status.HTTP_400_BAD_REQUEST
            )

        wanted = []
        seen = set()
        for index, row in enumerate(payload):
            try:
                story_id = int(row.get("story"))
            except (AttributeError, TypeError, ValueError):
                return Response(
                    {"detail": "Each item needs a numeric story id."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # A story twice in one journey is an editing slip, not a path.
            if story_id in seen:
                continue
            seen.add(story_id)
            wanted.append((story_id, index + 1, bool(row.get("required", True))))

        valid_ids = set(Story.objects.filter(id__in=seen).values_list("id", flat=True))

        with transaction.atomic():
            journey.items.exclude(story_id__in=valid_ids).delete()
            for story_id, position, required in wanted:
                if story_id not in valid_ids:
                    continue
                StoryJourneyItem.objects.update_or_create(
                    journey=journey,
                    story_id=story_id,
                    defaults={"position": position, "required": required},
                )

        return Response(
            AdminStoryJourneyItemSerializer(
                journey.items.select_related("story").all(),
                many=True,
                context={"request": request},
            ).data
        )


def _unique_mood_slug(name: str) -> str:
    base_slug = slugify(name) or "mood"
    slug = base_slug
    index = 2
    while Mood.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


class MoodAdminViewSet(ModelViewSet):
    """Management CRUD for the admin panel's Moods page.

    Mirrors TagAdminViewSet/ThemeAdminViewSet, with one addition the other
    taxonomies do not need: the assignment actions below, which exist because
    a mood assignment carries provenance (§8.5) and an AI suggestion has to be
    reviewable rather than merely present.
    """

    queryset = Mood.objects.all().order_by("order", "name")
    serializer_class = AdminMoodSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["name"]

    def perform_create(self, serializer):
        serializer.save(slug=_unique_mood_slug(serializer.validated_data["name"]))

    @action(detail=False, methods=["get"], url_path="pending-review")
    def pending_review(self, request):
        """Machine suggestions nobody has looked at yet.

        This queue is the whole point of storing `source`: without somewhere to
        see unreviewed suggestions, "allow admin review" is a database column
        rather than a workflow.
        """
        pending = (
            StoryMood.objects.filter(source=StoryMood.SOURCE_AI, reviewed=False)
            .select_related("story", "mood")
            .order_by("-created_at")
        )
        return Response(AdminStoryMoodSerializer(pending, many=True).data)

    @action(detail=False, methods=["get", "post"], url_path=r"assignments/(?P<story_id>\d+)")
    def assignments(self, request, story_id=None):
        """Read or replace one story's moods.

        POST replaces the whole set rather than adding to it, so the admin form
        can send what the editor sees. Assignments already present keep their
        provenance — re-saving a form must not silently relabel a reviewed AI
        suggestion as an administrator's own choice, or the review record would
        erase itself the first time anyone touched the story.
        """
        story = get_object_or_404(Story, pk=story_id)

        if request.method == "GET":
            existing = story.story_moods.select_related("mood").order_by("mood__order")
            return Response(AdminStoryMoodSerializer(existing, many=True).data)

        mood_ids = request.data.get("moods", [])
        if not isinstance(mood_ids, list):
            return Response(
                {"detail": "moods must be a list of mood ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        valid_ids = set(Mood.objects.filter(id__in=mood_ids).values_list("id", flat=True))

        with transaction.atomic():
            story.story_moods.exclude(mood_id__in=valid_ids).delete()
            existing_ids = set(story.story_moods.values_list("mood_id", flat=True))
            StoryMood.objects.bulk_create(
                [
                    StoryMood(story=story, mood_id=mood_id, source=StoryMood.SOURCE_ADMIN)
                    for mood_id in valid_ids - existing_ids
                ]
            )

        refreshed = story.story_moods.select_related("mood").order_by("mood__order")
        return Response(AdminStoryMoodSerializer(refreshed, many=True).data)

    @action(detail=False, methods=["post"], url_path=r"assignments/(?P<assignment_id>\d+)/review")
    def review_assignment(self, request, assignment_id=None):
        """Approve or reject one machine suggestion.

        Approving marks it reviewed, which is what makes it visible to readers;
        rejecting deletes it. Both are explicit decisions by a person, which is
        the whole of §8.5.
        """
        assignment = get_object_or_404(StoryMood, pk=assignment_id)
        approved = bool(request.data.get("approved"))

        if not approved:
            assignment.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        assignment.reviewed = True
        assignment.note = request.data.get("note", assignment.note)
        assignment.save(update_fields=["reviewed", "note"])
        return Response(AdminStoryMoodSerializer(assignment).data)


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


# The discovery endpoints run many ordered queries + card serialization (with
# per-row favourite/review counts) on every hit and are otherwise uncached, so
# a cold DB makes the homepage's loader/hydration fetch crawl in production.
# A short public cache absorbs traffic bursts and SSR without letting new
# stories / counts go visibly stale. Varied on Authorization so a logged-in
# reader never gets another visitor's cached "favourited" flags.
DISCOVERY_CACHE_SECONDS = 60


@method_decorator(cache_page(DISCOVERY_CACHE_SECONDS), name="get")
@method_decorator(vary_on_headers("Authorization"), name="get")
class HomeDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.published().for_card_list()
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

        # Manually-curated hero picks (superuser-set, see StoryAdminViewSet.featured)
        # replace the hero entirely, in their chosen order, however many there
        # are (1-5) — no backfill once at least one is picked. Only with zero
        # manual picks does the hero fall back to the automatic top-by-views pick.
        manual_featured = list(
            base_qs.filter(featured_rank__isnull=False).order_by("featured_rank")[:5]
        )
        # Videos share the lineup: same rank space, so the two lists merge into
        # one ordered set. A video only counts while it is watchable.
        watchable_videos = Video.objects.select_related("story", "video_type").filter(
            Q(story__isnull=True) | published_story_q("story")
        )
        manual_videos = list(watchable_videos.filter(featured_rank__isnull=False).order_by("featured_rank")[:5])
        if manual_featured or manual_videos:
            used_ids.update(story.id for story in manual_featured)
            featured_stories = manual_featured
            lineup = sorted(
                [("story", s.featured_rank, s) for s in manual_featured]
                + [("video", v.featured_rank, v) for v in manual_videos],
                key=lambda entry: entry[1],
            )[:5]
        else:
            featured_stories = take(base_qs.order_by("-views", "-rating", "-id"), 5)
            lineup = [("story", None, s) for s in featured_stories]
        featured_items = [
            {"kind": "story", "story": FeaturedStorySerializer(item, context={"request": request}).data}
            if kind == "story"
            else {"kind": "video", "video": PublicVideoSerializer(item, context={"request": request}).data}
            for kind, _rank, item in lineup
        ]
        # Dates are deliberately interpreted in Django's configured timezone,
        # which is UTC. Every visitor therefore receives the same story for a
        # given date, regardless of their device timezone. With no active
        # record, the hero's existing lead story is the graceful fallback.
        configured_daily = (
            DailyStory.objects.select_related("story", "story__author", "video", "video__story", "video__video_type")
            .filter(date=timezone.localdate(), active=True)
            .filter(Q(story__in=base_qs) | Q(video__in=watchable_videos))
            .first()
        )
        if configured_daily:
            daily_story = DailyStorySerializer(configured_daily, context={"request": request}).data
            daily_story["configured"] = True
        elif featured_items:
            lead = featured_items[0]
            daily_story = {
                "date": timezone.localdate().isoformat(),
                "story": lead.get("story"),
                "video": lead.get("video"),
                "featured_reason": "Today's featured pick" if lead["kind"] == "video" else "Today's featured story",
                "configured": False,
            }
        else:
            daily_story = None
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
        originals = list(
            with_preferred_translation_only(base_qs.filter(is_original=True))
            .order_by("-site_published_date", "-id")[:12]
        )

        # Cached for an hour (see home_metrics); includes the legacy
        # creators/readers keys for templates and clients that still read them.
        stats = get_home_stats()

        return Response(
            {
                "featured_stories": FeaturedStorySerializer(
                    featured_stories, many=True, context={"request": request}
                ).data,
                "featured_items": featured_items,
                "daily_story": daily_story,
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
                "originals": StoryListSerializer(
                    originals, many=True, context={"request": request}
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
                    "stats": stats,
                },
                "hero": serialize_hero(HeroTemplate.objects.current(), stats),
                "sections": resolve_home_sections(request),
            }
        )


def _story_cards(story_ids, request):
    """Card-list serializations for `story_ids`, in that order (missing ones skipped)."""
    stories = {s.pk: s for s in Story.objects.published().for_card_list().filter(pk__in=story_ids)}
    ordered = [stories[pk] for pk in story_ids if pk in stories]
    return ordered, StoryListSerializer(ordered, many=True, context={"request": request}).data


@method_decorator(cache_page(DISCOVERY_CACHE_SECONDS), name="get")
@method_decorator(vary_on_headers("Authorization"), name="get")
class DiscoverDataAPIView(APIView):
    """The Discover page: what is trending this week, hidden gems, what was just
    added, where the stories come from, and the biggest genres. See
    apps/story/discover_api.py for the rules behind each list."""

    def get(self, request):
        from . import discover_api

        trending_rows = discover_api.get_trending()
        _, trending_cards = _story_cards([row["story_id"] for row in trending_rows], request)
        card_by_id = {card["id"]: card for card in trending_cards}
        trending = [
            {
                "story": card_by_id[row["story_id"]],
                "readers": row["readers"],
                "window_days": row["window_days"],
                "rank_change": row["rank_change"],
            }
            for row in trending_rows
            if row["story_id"] in card_by_id
        ]

        gem_rows = discover_api.get_hidden_gems()
        _, gem_cards = _story_cards([row["story_id"] for row in gem_rows], request)
        gem_by_id = {card["id"]: card for card in gem_cards}
        hidden_gems = [
            {"story": gem_by_id[row["story_id"]], "reason": row["reason"]}
            for row in gem_rows
            if row["story_id"] in gem_by_id
        ]

        added = discover_api.compute_just_added()
        _, added_cards = _story_cards([story_id for story_id, _fmt in added], request)
        added_by_id = {card["id"]: card for card in added_cards}
        just_added = [
            {"story": added_by_id[story_id], "format": fmt} for story_id, fmt in added if story_id in added_by_id
        ]

        return Response(
            {
                "trending": trending,
                "hidden_gems": hidden_gems,
                "just_added": just_added,
                "countries": discover_api.compute_countries(request),
                "popular_genres": discover_api.compute_popular_genres(),
            }
        )


class DiscoverSurpriseAPIView(APIView):
    """One random story worth opening: published, with a real cover and a
    description. `?exclude=1,2,3` (ids) keeps the ones already shown out; when
    everything has been shown the pool is reused and `recycled` is true."""

    def get(self, request):
        from . import discover_api

        exclude = [int(token) for token in request.query_params.get("exclude", "").split(",") if token.strip().isdigit()]
        pool = discover_api.surprise_pool(exclude)
        recycled = False
        if not pool.exists():
            recycled = bool(exclude)
            pool = discover_api.surprise_pool()
        story = pool.order_by("?").values_list("pk", flat=True).first()
        if story is None:
            return Response({"story": None, "blurb": "", "recycled": False})
        stories, cards = _story_cards([story], request)
        return Response(
            {"story": cards[0] if cards else None, "blurb": discover_api.blurb_for(stories[0]) if stories else "", "recycled": recycled}
        )


class StoryJourneyListAPIView(APIView):
    """Active journeys, with this reader's progress through each.

    Progress is derived from completions rather than stored — see
    apps/stats/journeys.py. A signed-out reader gets the same list with zero
    progress, so the page is worth visiting before signing in.
    """

    def get(self, request):
        rows = journeys_for_reader(request.user)
        return Response(
            {
                "journeys": [
                    {
                        "slug": row["journey"].slug,
                        "title": row["journey"].title,
                        "description": row["journey"].description,
                        "type": row["journey"].type,
                        "cover_image": _journey_cover(row["journey"], request),
                        # Up to two story covers, for the Discover card's collage.
                        "covers": _journey_covers(row["journey"], request),
                        "completed": row["completed"],
                        "total": row["total"],
                        "is_complete": row["is_complete"],
                        "next_story": _journey_next_story(
                            row["journey"], row["completed_ids"], request
                        ),
                    }
                    for row in rows
                ]
            }
        )


def _journey_covers(journey, request, limit=2):
    return [
        get_cover_image_url(item.story.cover_image_file, item.story.cover_image, request, size=CARD_COVER_SIZE)
        for item in list(journey.items.all())[:limit]
    ]


def _journey_cover(journey, request):
    """A journey's own artwork, or the first story's cover.

    Falling back means a journey is never blank simply because nobody uploaded
    a bespoke image for it.
    """
    if journey.cover_image:
        return journey.cover_image
    first = next(iter(journey.items.all()), None)
    if not first:
        return ""
    return get_cover_image_url(
        first.story.cover_image_file, first.story.cover_image, request, size=CARD_COVER_SIZE
    )


def _journey_next_story(journey, completed_ids, request):
    """First unfinished required published story, using prefetched items."""
    for item in journey.items.all():
        story = item.story
        if not item.required or item.story_id in completed_ids or not story.is_published:
            continue
        return {
            "slug": story.slug,
            "title": story.title,
            "cover_image": get_cover_image_url(
                story.cover_image_file, story.cover_image, request, size=CARD_COVER_SIZE
            ),
        }
    return None


class StoryJourneyDetailAPIView(APIView):
    """One journey, in order, with each story marked done or not."""

    def get(self, request, slug):
        journey = get_object_or_404(
            StoryJourney.objects.filter(active=True).prefetch_related("items__story"), slug=slug
        )
        completed_ids = _reader_completed_story_ids(request.user)
        done, total, is_complete = journey_progress(journey, completed_ids)
        if total == 0:
            raise Http404("This journey has no stories yet.")

        items = list(journey.items.all())
        cards = {
            story.id: story
            for story in Story.objects.published().filter(
                id__in=[item.story_id for item in items]
            ).for_card_list()
        }

        return Response(
            {
                "slug": journey.slug,
                "title": journey.title,
                "description": journey.description,
                "type": journey.type,
                "cover_image": _journey_cover(journey, request),
                "completed": done,
                "total": total,
                "is_complete": is_complete,
                "next_story": _journey_next_story(journey, completed_ids, request),
                "items": [
                    {
                        "position": item.position,
                        "required": item.required,
                        "completed": item.story_id in completed_ids,
                        "story": StoryListSerializer(
                            cards[item.story_id], context={"request": request}
                        ).data,
                    }
                    for item in items
                    # A story unpublished since the journey was built is not shown
                    # (and does not count toward the journey: see journey_progress).
                    if item.story_id in cards
                ],
            }
        )


def _reader_completed_story_ids(user):
    if not user or not user.is_authenticated:
        return set()
    return set(
        StoryCompletion.objects.filter(user=user).values_list("story_id", flat=True)
    )


class MoodListAPIView(APIView):
    """The mood vocabulary, with how many published stories each one has.

    Counts only assignments readers may see (see `public_story_mood_q`), so an
    unreviewed AI classification cannot inflate a count and lead someone to a
    mood whose stories then do not appear.
    """

    def get(self, request):
        moods = (
            Mood.objects.filter(active=True)
            .annotate(
                stories_count=Count(
                    "story_moods",
                    filter=Q(
                        story_moods__story__in=Story.objects.published(),
                    )
                    & (
                        Q(story_moods__source=StoryMood.SOURCE_ADMIN)
                        | Q(story_moods__reviewed=True)
                    ),
                    distinct=True,
                )
            )
            .order_by("order", "name")
        )
        return Response(
            {
                "moods": [
                    {
                        "id": mood.id,
                        "slug": mood.slug,
                        "name": mood.name,
                        "icon": mood.icon,
                        "description": mood.description,
                        "stories_count": mood.stories_count,
                    }
                    for mood in moods
                ]
            }
        )


class StoryMapAPIView(APIView):
    """Published story totals grouped by ISO 3166-1 alpha-2 country code."""

    @method_decorator(cache_page(60 * 15))
    def get(self, request):
        country_names = dict(COUNTRY_CHOICES)
        grouped_counts = list(
            Story.objects.published()
            .exclude(country="")
            .values("country")
            .annotate(stories_count=Count("id"))
            .order_by("-stories_count", "country")
        )
        author_rows = (
            Story.objects.published()
            .exclude(country="")
            .filter(author__isnull=False, author__is_traditional=False, author__is_house=False)
            .exclude(author__name="")
            .values("country", "author__name", "author__slug")
            .annotate(stories_count=Count("id"))
            .order_by("country", "-stories_count", "author__name", "author__id")
        )
        authors_by_country = {}
        for row in author_rows:
            authors = authors_by_country.setdefault(row["country"], [])
            if len(authors) < 4:
                authors.append({"name": row["author__name"], "slug": row["author__slug"]})

        journey_rows = (
            StoryJourney.objects.filter(
                active=True,
                type=StoryJourney.TYPE_COUNTRY,
                items__required=True,
            )
            .values("id", "title", "slug", "order")
            .annotate(
                required_count=Count("items", filter=Q(items__required=True), distinct=True),
                countries_count=Count(
                    "items__story__country",
                    filter=Q(items__required=True),
                    distinct=True,
                ),
                country=Min("items__story__country", filter=Q(items__required=True)),
            )
            .filter(required_count__gt=0, countries_count=1)
            .exclude(country="")
            .order_by("order", "title", "id")
        )
        journeys_by_country = {}
        for row in journey_rows:
            journeys_by_country.setdefault(
                row["country"],
                {"title": row["title"], "slug": row["slug"]},
            )

        countries = [
            {
                "code": item["country"],
                "name": country_names.get(item["country"], item["country"]),
                "stories_count": item["stories_count"],
                "region": story_map_region(item["country"]),
                "top_authors": authors_by_country.get(item["country"], []),
                "journey": journeys_by_country.get(item["country"]),
            }
            for item in grouped_counts
        ]
        region_totals = {
            name: {"name": name, "stories_count": 0, "countries_count": 0}
            for name in REGION_ORDER
        }
        for country in countries:
            region = region_totals[country["region"]]
            region["stories_count"] += country["stories_count"]
            region["countries_count"] += 1
        return Response(
            {
                "countries": countries,
                "regions": [region_totals[name] for name in REGION_ORDER if region_totals[name]["countries_count"]],
                "total_stories": sum(item["stories_count"] for item in countries),
                "countries_count": len(countries),
                "max_stories_count": max(
                    (item["stories_count"] for item in countries),
                    default=0,
                ),
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
                .for_card_list()
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

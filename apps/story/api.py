import re
import uuid

from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from django.db.models import Sum, Avg, Count, F
from django.db.models import Q
from django.http import FileResponse
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from datetime import timedelta
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import BasePermission
from rest_framework.filters import SearchFilter

from apps.story.filters import StoryFilter
from apps.stats.models import ReadingProgress, AudioReadingProgress
from apps.users.models import User
from .models import (
    Genre,
    Story,
    Chapter,
    Audio,
    Author,
    Review,
    Favorite,
    Submission,
    StoryView,
    with_preferred_translation_only,
    published_story_q,
)
from .serializers import (
    GenreSerializer,
    AdminGenreSerializer,
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


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def is_bot_request(request):
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return bool(BOT_USER_AGENT_PATTERN.search(user_agent))


class AdminChapterPagination(PageNumberPagination):
    page_size = 10000


class CataloguePagination(PageNumberPagination):
    page_size = 12


class LibraryShelfPagination(PageNumberPagination):
    page_size = 4


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class StoryViewSet(ReadOnlyModelViewSet):
    queryset = Story.objects.published().order_by("-id")
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend]
    filterset_class = StoryFilter
    pagination_class = CataloguePagination

    def get_queryset(self):
        queryset = super().get_queryset()
        # Only the "list" action (the public browse/search listing) collapses
        # each translation_group down to one edition — retrieve and the other
        # detail actions (chapter, favorite, reviews, etc.) must still resolve
        # any specific translation directly by its own slug, e.g. for the
        # Translations panel's "switch language" links to keep working.
        if self.action == "list":
            queryset = with_preferred_translation_only(queryset)
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
        """Same-origin proxy for an audio chapter's bytes — used only by the
        offline-download flow so it can fetch bytes to encrypt without
        depending on the R2 bucket's CORS config (unlike normal playback,
        which reads audio_file's direct R2 URL and doesn't need CORS since
        it's a plain <audio> resource load, not a script-readable fetch)."""
        story = self.get_object()
        audio = story.audios.filter(slug=audio_slug).first()
        if not audio or not audio.audio_file:
            return Response({"detail": "Audio file not available."}, status=status.HTTP_404_NOT_FOUND)
        file_obj = audio.audio_file.open("rb")
        return FileResponse(file_obj, content_type="audio/mpeg")


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


class StoryAdminViewSet(ModelViewSet):
    queryset = Story.objects.select_related("author", "submitted_by", "submission").all().order_by("-id")
    serializer_class = StoryAdminSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "about"]

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


class ChapterAdminViewSet(ModelViewSet):
    queryset = Chapter.objects.select_related("story").all().order_by("story_id", "order")
    serializer_class = ChapterAdminSerializer
    pagination_class = AdminChapterPagination
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug", "story__title"]

    def get_queryset(self):
        queryset = super().get_queryset()
        story_id = self.request.query_params.get("story")
        if story_id:
            queryset = queryset.filter(story_id=story_id).order_by("order", "id")
        return queryset


class AudioAdminViewSet(ModelViewSet):
    queryset = Audio.objects.select_related("story").all().order_by("story_id", "order")
    serializer_class = AudioAdminSerializer
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


class AdminAuthorListAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        authors = Author.objects.all().order_by("name")
        serializer = AdminAuthorSerializer(authors, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = AdminAuthorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        author = serializer.save()
        return Response(AdminAuthorSerializer(author).data, status=status.HTTP_201_CREATED)


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
        base_qs = Story.objects.published().prefetch_related("genres", "audios")
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
        base_qs = Story.objects.published().prefetch_related("genres", "audios")
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
        base_qs = Story.objects.published().prefetch_related("genres", "audios")
        genres = Genre.objects.all()
        return Response(
            {
                "genres": GenreSerializer(genres, many=True).data,
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


class SearchStoryAPIView(ListAPIView):
    serializer_class = StoryListSerializer

    def get_queryset(self):
        q = self.request.query_params.get("q", "").strip()
        sort = self.request.query_params.get("sort", "popular").lower()
        language = self.request.query_params.get("language", "").strip()

        if not q:
            return Story.objects.none()

        queryset = (
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

        if language and language.lower() != "all":
            queryset = queryset.filter(language=language)

        if sort == "recent":
            return queryset.order_by("-site_published_date", "-id")
        if sort == "rating":
            return queryset.order_by("-rating", "-views", "-id")
        if sort == "views":
            return queryset.order_by("-views", "-rating", "-id")
        return queryset.order_by("-views", "-rating", "-id")

from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from django.db.models import Sum, Avg
from django.db.models import Q
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser

from apps.story.filters import StoryFilter
from .models import Genre, Story, Chapter, Audio, Author, Review, Favorite, Submission
from .serializers import (
    GenreSerializer,
    StoryListSerializer,
    StoryDetailSerializer,
    ChapterSerializer,
    AudioSerializer,
    ReviewSerializer,
    ReviewWriteSerializer,
    SubmissionSerializer,
    SubmissionListSerializer,
)
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.permissions import IsAuthenticated
from rest_framework import status


class StoryViewSet(ReadOnlyModelViewSet):
    queryset = Story.objects.all().order_by("-id")
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend]
    filterset_class = StoryFilter

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


class SubmissionViewSet(ModelViewSet):
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = Submission.objects.select_related("user").prefetch_related("genres")
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == "list":
            return SubmissionListSerializer
        return SubmissionSerializer


class GenreListAPIView(APIView):
    def get(self, request):
        genres = Genre.objects.all()
        serializer = GenreSerializer(genres, many=True)
        return Response(serializer.data)


class HomeDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.all().prefetch_related("genres", "audios")

        weekly_spotlight = base_qs.order_by("-rating", "-views", "-id")[:6]
        new_trending = base_qs.order_by("-views", "-published_date", "-id")[:5]
        recommended = base_qs.order_by("-rating", "-published_date", "-id")[:6]
        popular = base_qs.order_by("-views", "-rating", "-id")[:6]
        originals = base_qs.order_by("-id")[:6]
        new_releases = base_qs.order_by("-published_date", "-id")[:6]
        sidebar_recommended = base_qs.order_by("-rating", "-views", "-id")[:3]
        featured_story = base_qs.order_by("-views", "-rating", "-id").first()

        readers_count = (
            Story.objects.aggregate(total_readers=Sum("views")).get("total_readers") or 0
        )

        return Response(
            {
                "featured_story": (
                    StoryListSerializer(featured_story, context={"request": request}).data
                    if featured_story
                    else None
                ),
                "weekly_spotlight": StoryListSerializer(
                    weekly_spotlight, many=True, context={"request": request}
                ).data,
                "new_trending": StoryListSerializer(
                    new_trending, many=True, context={"request": request}
                ).data,
                "tabs": {
                    "recommended": StoryListSerializer(
                        recommended, many=True, context={"request": request}
                    ).data,
                    "popular": StoryListSerializer(
                        popular, many=True, context={"request": request}
                    ).data,
                    "originals": StoryListSerializer(
                        originals, many=True, context={"request": request}
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
                        "stories": Story.objects.count(),
                        "readers": readers_count,
                    },
                },
            }
        )


class TrendingDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.all().prefetch_related("genres", "audios")
        return Response(
            {
                "today": StoryListSerializer(
                    base_qs.order_by("-views", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "week": StoryListSerializer(
                    base_qs.order_by("-views", "-rating", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "month": StoryListSerializer(
                    base_qs.order_by("-rating", "-views", "-id")[:10],
                    many=True,
                    context={"request": request},
                ).data,
                "alltime": StoryListSerializer(
                    base_qs.order_by("-views", "-rating", "-published_date")[:10],
                    many=True,
                    context={"request": request},
                ).data,
            }
        )


class OriginalsDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.all().prefetch_related("genres", "audios")
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
        base_qs = Story.objects.all().prefetch_related("genres", "audios")
        genres = Genre.objects.all()
        return Response(
            {
                "genres": GenreSerializer(genres, many=True).data,
                "new_releases": StoryListSerializer(
                    base_qs.order_by("-published_date", "-id")[:20],
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


class SearchStoryAPIView(ListAPIView):
    serializer_class = StoryListSerializer

    def get_queryset(self):
        q = self.request.query_params.get("q", "").strip()
        sort = self.request.query_params.get("sort", "popular").lower()

        if not q:
            return Story.objects.none()

        queryset = (
            Story.objects.all()
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

        if sort == "recent":
            return queryset.order_by("-published_date", "-id")
        if sort == "rating":
            return queryset.order_by("-rating", "-views", "-id")
        if sort == "views":
            return queryset.order_by("-views", "-rating", "-id")
        return queryset.order_by("-views", "-rating", "-id")

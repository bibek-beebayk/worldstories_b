from rest_framework.viewsets import ReadOnlyModelViewSet
from rest_framework.views import APIView
from django.db.models import Sum

from apps.story.filters import StoryFilter
from .models import Genre, Story, Chapter, Audio, Author
from .serializers import GenreSerializer, StoryListSerializer, StoryDetailSerializer, ChapterSerializer, AudioSerializer
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend


class StoryViewSet(ReadOnlyModelViewSet):
    queryset = Story.objects.all().order_by("-id")
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend]
    filterset_class = StoryFilter

    def get_serializer_class(self):
        if self.action == "retrieve":
            return StoryDetailSerializer
        return StoryListSerializer

    @action(detail=True, methods=["get"], url_path=r"(?P<chapter_slug>[^/.]+)")
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
                    StoryListSerializer(featured_story).data if featured_story else None
                ),
                "weekly_spotlight": StoryListSerializer(weekly_spotlight, many=True).data,
                "new_trending": StoryListSerializer(new_trending, many=True).data,
                "tabs": {
                    "recommended": StoryListSerializer(recommended, many=True).data,
                    "popular": StoryListSerializer(popular, many=True).data,
                    "originals": StoryListSerializer(originals, many=True).data,
                    "new": StoryListSerializer(new_releases, many=True).data,
                },
                "sidebar": {
                    "recommended": StoryListSerializer(
                        sidebar_recommended, many=True
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
                    base_qs.order_by("-views", "-id")[:10], many=True
                ).data,
                "week": StoryListSerializer(
                    base_qs.order_by("-views", "-rating", "-id")[:10], many=True
                ).data,
                "month": StoryListSerializer(
                    base_qs.order_by("-rating", "-views", "-id")[:10], many=True
                ).data,
                "alltime": StoryListSerializer(
                    base_qs.order_by("-views", "-rating", "-published_date")[:10],
                    many=True,
                ).data,
            }
        )


class OriginalsDataAPIView(APIView):
    def get(self, request):
        base_qs = Story.objects.all().prefetch_related("genres", "audios")
        return Response(
            {
                "stories": StoryListSerializer(
                    base_qs.order_by("-id", "-rating")[:20], many=True
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
                    base_qs.order_by("-published_date", "-id")[:20], many=True
                ).data,
                "hidden_gems": StoryListSerializer(
                    base_qs.order_by("-rating", "views", "-id")[:20], many=True
                ).data,
            }
        )

from rest_framework.viewsets import ReadOnlyModelViewSet
from rest_framework.views import APIView

from apps.story.filters import StoryFilter
from .models import Genre, Story, Chapter, Audio
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
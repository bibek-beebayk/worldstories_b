from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.throttling import SimpleRateThrottle

from apps.story.models import Blog, Story
from apps.stats.models import (
    AnalyticsEvent,
    ReadingProgress,
    ChapterReadingProgress,
    AudioReadingProgress,
    BlogReadingProgress,
    FileReadingProgress,
)
from apps.stats.serializers import (
    AnalyticsEventWriteSerializer,
    ReadingProgressSerializer,
    ReadingProgressWriteSerializer,
    AudioReadingProgressSerializer,
    AudioReadingProgressWriteSerializer,
    BlogReadingProgressSerializer,
    BlogReadingProgressWriteSerializer,
    FileReadingProgressSerializer,
    FileReadingProgressWriteSerializer,
)


class AnalyticsEventThrottle(SimpleRateThrottle):
    scope = "analytics-events"

    def get_rate(self):
        return "120/min"

    def get_cache_key(self, request, view):
        if request.user.is_authenticated:
            ident = str(request.user.pk)
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class AnalyticsEventCreateAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AnalyticsEventThrottle]

    def post(self, request):
        serializer = AnalyticsEventWriteSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        event = serializer.save()
        return Response(
            {"event_id": str(event.event_id)}, status=status.HTTP_201_CREATED
        )


class ReadingProgressAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _build_progress_payload(self, request, story, progress):
        chapter_progress_qs = ChapterReadingProgress.objects.filter(
            user=request.user, story=story
        ).select_related("chapter")
        chapter_progress_map = {
            item.chapter.slug: max(0.0, min(1.0, item.progress))
            for item in chapter_progress_qs
            if item.chapter and item.chapter.slug
        }

        total_chapters = story.chapters.count()
        overall_progress = 0.0
        if total_chapters > 0:
            overall_progress = sum(chapter_progress_map.values()) / total_chapters

        serializer = ReadingProgressSerializer(
            progress,
            context={
                "chapter_progress_map": chapter_progress_map,
                "overall_progress": round(overall_progress, 4),
            },
        )
        return serializer.data

    def get(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        progress = ReadingProgress.objects.filter(user=request.user, story=story).first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._build_progress_payload(request, story, progress))

    def put(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        serializer = ReadingProgressWriteSerializer(
            data=request.data,
            context={"story": story},
        )
        serializer.is_valid(raise_exception=True)
        progress_obj, _ = ReadingProgress.objects.get_or_create(
            user=request.user,
            story=story,
        )
        progress_obj.chapter = serializer.validated_data.get("chapter")
        progress_obj.progress = serializer.validated_data["progress"]
        progress_obj.last_element_id = serializer.validated_data.get("last_element_id", "")
        progress_obj.save()

        chapter = serializer.validated_data.get("chapter")
        if chapter:
            chapter_progress_obj, _ = ChapterReadingProgress.objects.get_or_create(
                user=request.user,
                story=story,
                chapter=chapter,
            )
            chapter_progress_obj.progress = max(
                chapter_progress_obj.progress, serializer.validated_data["progress"]
            )
            chapter_progress_obj.save()

        return Response(self._build_progress_payload(request, story, progress_obj))


class AudioReadingProgressAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _build_progress_payload(self, request, story, progress):
        audio_progress_qs = AudioReadingProgress.objects.filter(
            user=request.user, story=story
        ).select_related("audio")
        audio_progress_map = {
            item.audio.slug: {
                "progress": max(0.0, min(1.0, item.progress)),
                "position_seconds": max(0.0, item.position_seconds),
                "duration_seconds": max(0.0, item.duration_seconds),
            }
            for item in audio_progress_qs
            if item.audio and item.audio.slug
        }

        total_audios = story.audios.count()
        overall_progress = 0.0
        if total_audios > 0:
            overall_progress = sum(
                item["progress"] for item in audio_progress_map.values()
            ) / total_audios

        serializer = AudioReadingProgressSerializer(
            progress,
            context={
                "audio_progress_map": audio_progress_map,
                "overall_progress": round(overall_progress, 4),
            },
        )
        return serializer.data

    def get(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        progress = AudioReadingProgress.objects.filter(
            user=request.user, story=story
        ).order_by("-updated_at").first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._build_progress_payload(request, story, progress))

    def put(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        serializer = AudioReadingProgressWriteSerializer(
            data=request.data,
            context={"story": story},
        )
        serializer.is_valid(raise_exception=True)

        audio = serializer.validated_data.get("audio")
        if not audio:
            return Response(
                {"detail": "audio_slug is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        progress_obj, _ = AudioReadingProgress.objects.get_or_create(
            user=request.user,
            story=story,
            audio=audio,
        )
        current_progress = serializer.validated_data["progress"]
        progress_obj.progress = max(progress_obj.progress, current_progress)
        progress_obj.position_seconds = serializer.validated_data.get("position_seconds", 0.0)
        progress_obj.duration_seconds = serializer.validated_data.get("duration_seconds", 0.0)
        progress_obj.save()

        return Response(self._build_progress_payload(request, story, progress_obj))


class BlogReadingProgressAPIView(APIView):
    """Tracks scroll-depth progress through a blog post — one row per
    (user, blog), no chapter-style breakdown since posts are single-page."""

    permission_classes = [IsAuthenticated]

    def get(self, request, blog_slug):
        blog = get_object_or_404(Blog.objects.published(), slug=blog_slug)
        progress = BlogReadingProgress.objects.filter(user=request.user, blog=blog).first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(BlogReadingProgressSerializer(progress).data)

    def put(self, request, blog_slug):
        blog = get_object_or_404(Blog.objects.published(), slug=blog_slug)
        serializer = BlogReadingProgressWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        progress_obj, _ = BlogReadingProgress.objects.get_or_create(user=request.user, blog=blog)
        # Only ever moves forward — a reader jumping back up to re-read an
        # earlier section shouldn't erase how far they'd actually gotten.
        progress_obj.progress = max(progress_obj.progress, serializer.validated_data["progress"])
        progress_obj.save()

        return Response(BlogReadingProgressSerializer(progress_obj).data)


class FileReadingProgressAPIView(APIView):
    """Tracks progress through a story's EPUB/PDF file — one row per
    (user, story, format), unlike chapters/audios there's only ever one file
    of a given format per story so no per-item breakdown is needed."""

    permission_classes = [IsAuthenticated]

    def get(self, request, story_slug, file_format):
        if file_format not in dict(FileReadingProgress.FORMAT_CHOICES):
            return Response({"detail": "Invalid format."}, status=status.HTTP_400_BAD_REQUEST)
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        progress = FileReadingProgress.objects.filter(
            user=request.user, story=story, format=file_format
        ).first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(FileReadingProgressSerializer(progress).data)

    def put(self, request, story_slug, file_format):
        if file_format not in dict(FileReadingProgress.FORMAT_CHOICES):
            return Response({"detail": "Invalid format."}, status=status.HTTP_400_BAD_REQUEST)
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        serializer = FileReadingProgressWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        progress_obj, _ = FileReadingProgress.objects.get_or_create(
            user=request.user,
            story=story,
            format=file_format,
        )
        progress_obj.progress = max(progress_obj.progress, serializer.validated_data["progress"])
        progress_obj.position = serializer.validated_data.get("position", progress_obj.position)
        progress_obj.save()

        return Response(FileReadingProgressSerializer(progress_obj).data)

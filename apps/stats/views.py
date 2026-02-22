from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from apps.story.models import Story
from apps.stats.models import ReadingProgress, ChapterReadingProgress
from apps.stats.serializers import (
    ReadingProgressSerializer,
    ReadingProgressWriteSerializer,
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
        story = get_object_or_404(Story, slug=story_slug)
        progress = ReadingProgress.objects.filter(user=request.user, story=story).first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._build_progress_payload(request, story, progress))

    def put(self, request, story_slug):
        story = get_object_or_404(Story, slug=story_slug)
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

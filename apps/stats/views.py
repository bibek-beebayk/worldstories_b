import logging
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

logger = logging.getLogger(__name__)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.throttling import SimpleRateThrottle

from apps.stats.achievements import evaluate as evaluate_achievements, serialize_earned
from apps.stats.completion import record_completion_if_finished
from apps.story.api import is_untracked_request
from apps.story.models import Audiobook, Blog, Story, Video, published_story_q
from apps.stats.models import (
    AnalyticsEvent,
    ReadingProgress,
    ChapterReadingProgress,
    AudioReadingProgress,
    VideoWatchProgress,
    BlogReadingProgress,
    QuickReadProgress,
    FileReadingProgress,
    StoryCompletion,
)
from apps.stats.serializers import (
    AnalyticsEventWriteSerializer,
    ReadingProgressSerializer,
    ReadingProgressWriteSerializer,
    AudioReadingProgressSerializer,
    AudioReadingProgressWriteSerializer,
    AudiobookProgressWriteSerializer,
    VideoWatchProgressSerializer,
    VideoProgressWriteSerializer,
    VideoWatchProgressWriteSerializer,
    BlogReadingProgressSerializer,
    BlogReadingProgressWriteSerializer,
    QuickReadProgressSerializer,
    QuickReadProgressWriteSerializer,
    FileReadingProgressSerializer,
    FileReadingProgressWriteSerializer,
)


def annotate_completion(payload, completion):
    """Add the one-shot completion signals to a progress response.

    `story_completed` is true only on the write that finished the story, and
    `unlocked_country` only when that finish also reached a country the reader
    had never completed anything from. Both are facts about *this response*
    rather than about the reader's state, which is what lets the client show a
    completion screen or an unlock toast exactly once.
    """
    payload["story_completed"] = bool(completion)
    payload["unlocked_country"] = getattr(completion, "unlocked_country", None)
    payload["unlocked_achievements"] = serialize_earned(
        getattr(completion, "unlocked_achievements", []) or []
    )
    return payload


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
        # Crawlers and superusers are dropped before anything is written, so the
        # events table only ever holds real audience behaviour from the reader
        # side of the site. Answered 202 rather than an error: the client is a
        # fire-and-forget beacon with nothing useful to do about a rejection,
        # and a 4xx here would just show up as console noise for admins
        # browsing their own site.
        if is_untracked_request(request):
            return Response(status=status.HTTP_202_ACCEPTED)

        serializer = AnalyticsEventWriteSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        event = serializer.save()

        # The one event type that moves an achievement counter and has no
        # server-side write of its own to hang off. Everything else is
        # triggered where it happens — see apps/stats/achievements.py.
        unlocked = []
        if event.event_type == AnalyticsEvent.EVENT_QUICK_READ_COMPLETED:
            unlocked = evaluate_achievements(request.user, "quick_read_completed")

        return Response(
            {
                "event_id": str(event.event_id),
                "unlocked_achievements": serialize_earned(unlocked),
            },
            status=status.HTTP_201_CREATED,
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

        payload = self._build_progress_payload(request, story, progress_obj)
        # Settled server-side on the same request that moved the progress, so
        # it survives a cleared browser store and a second device.
        annotate_completion(payload, record_completion_if_finished(request.user, story))
        return Response(payload)


class AudioReadingProgressAPIView(APIView):
    """Deprecated compatibility wrapper around AudiobookProgressAPIView.

    Resolves the story's first published audiobook and delegates to the
    audiobook-keyed progress logic while logging a deprecation warning.
    """

    permission_classes = [IsAuthenticated]

    def _resolve_audiobook(self, request, story_slug):
        logger.warning(
            "Deprecated story-keyed audio progress endpoint called: %s",
            request.path,
        )
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        audiobook = (
            Audiobook.objects.published()
            .filter(story=story)
            .order_by("id")
            .first()
        )
        return audiobook

    def get(self, request, story_slug):
        audiobook = self._resolve_audiobook(request, story_slug)
        if audiobook is None:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return AudiobookProgressAPIView().get(request, audiobook.slug)

    def put(self, request, story_slug):
        audiobook = self._resolve_audiobook(request, story_slug)
        if audiobook is None:
            return Response({"detail": "Audiobook not found."}, status=status.HTTP_404_NOT_FOUND)
        return AudiobookProgressAPIView().put(request, audiobook.slug)


class AudiobookProgressAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _collect_rows(self, user, audiobook):
        """Return audiobook-keyed progress rows for this user and audiobook."""
        track_ids = set(audiobook.tracks.values_list("id", flat=True))
        if not track_ids:
            return []
        return list(
            AudioReadingProgress.objects.filter(
                user=user,
                audiobook=audiobook,
                audio_id__in=track_ids,
            )
            .select_related("audio")
            .order_by("-updated_at")
        )

    def _build_progress_payload(self, audiobook, rows, latest):
        audio_progress_map = {
            item.audio.slug: {
                "audio_id": item.audio_id,
                "progress": max(0.0, min(1.0, item.progress)),
                "position_seconds": max(0.0, item.position_seconds),
                "duration_seconds": max(0.0, item.duration_seconds),
            }
            for item in rows
            if item.audio and item.audio.slug
        }

        total_tracks = audiobook.tracks.count()
        overall_progress = 0.0
        if total_tracks > 0:
            overall_progress = sum(
                item["progress"] for item in audio_progress_map.values()
            ) / total_tracks

        serializer = AudioReadingProgressSerializer(
            latest,
            context={
                "audio_progress_map": audio_progress_map,
                "overall_progress": round(overall_progress, 4),
            },
        )
        return serializer.data

    def get(self, request, audiobook_slug):
        audiobook = get_object_or_404(Audiobook.objects.published(), slug=audiobook_slug)
        rows = self._collect_rows(request.user, audiobook)
        if not rows:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._build_progress_payload(audiobook, rows, rows[0]))

    def put(self, request, audiobook_slug):
        audiobook = get_object_or_404(Audiobook.objects.published(), slug=audiobook_slug)
        serializer = AudiobookProgressWriteSerializer(
            data=request.data,
            context={"audiobook": audiobook},
        )
        serializer.is_valid(raise_exception=True)
        audio = serializer.validated_data["audio"]

        # Do not write legacy `story` on new rows; `audiobook` is the canonical key.
        progress_obj, _ = AudioReadingProgress.objects.get_or_create(
            user=request.user,
            audio=audio,
            defaults={"audiobook": audiobook},
        )
        progress_obj.audiobook = audiobook
        current_progress = serializer.validated_data["progress"]
        progress_obj.progress = max(progress_obj.progress, current_progress)
        progress_obj.position_seconds = serializer.validated_data.get("position_seconds", 0.0)
        progress_obj.duration_seconds = serializer.validated_data.get("duration_seconds", 0.0)
        progress_obj.save()

        rows = self._collect_rows(request.user, audiobook)
        payload = self._build_progress_payload(audiobook, rows, progress_obj)
        if audiobook.story_id is not None:
            annotate_completion(
                payload, record_completion_if_finished(request.user, audiobook.story)
            )
        return Response(payload)


class VideoWatchProgressAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _build_progress_payload(self, request, story, progress):
        video_progress_qs = VideoWatchProgress.objects.filter(
            user=request.user, story=story
        ).select_related("video")
        video_progress_map = {
            item.video.slug: {
                "progress": max(0.0, min(1.0, item.progress)),
                "position_seconds": max(0.0, item.position_seconds),
                "duration_seconds": max(0.0, item.duration_seconds),
            }
            for item in video_progress_qs
            if item.video and item.video.slug
        }

        total_videos = story.videos.count()
        overall_progress = 0.0
        if total_videos > 0:
            overall_progress = sum(
                item["progress"] for item in video_progress_map.values()
            ) / total_videos

        serializer = VideoWatchProgressSerializer(
            progress,
            context={
                "video_progress_map": video_progress_map,
                "overall_progress": round(overall_progress, 4),
            },
        )
        return serializer.data

    def get(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        progress = VideoWatchProgress.objects.filter(
            user=request.user, story=story
        ).order_by("-updated_at").first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._build_progress_payload(request, story, progress))

    def put(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        serializer = VideoWatchProgressWriteSerializer(
            data=request.data,
            context={"story": story},
        )
        serializer.is_valid(raise_exception=True)

        video = serializer.validated_data.get("video")
        if not video:
            return Response(
                {"detail": "video_slug is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        progress_obj, _ = VideoWatchProgress.objects.get_or_create(
            user=request.user,
            story=story,
            video=video,
        )
        current_progress = serializer.validated_data["progress"]
        progress_obj.progress = max(progress_obj.progress, current_progress)
        progress_obj.position_seconds = serializer.validated_data.get("position_seconds", 0.0)
        progress_obj.duration_seconds = serializer.validated_data.get("duration_seconds", 0.0)
        progress_obj.save()

        return Response(self._build_progress_payload(request, story, progress_obj))


class VideoProgressByIdAPIView(APIView):
    """Watch progress keyed on the video itself — works for standalone videos
    and story-linked ones alike, no story slug needed."""

    permission_classes = [IsAuthenticated]

    def _get_video(self, video_id):
        return get_object_or_404(
            Video.objects.select_related("story").filter(
                Q(story__isnull=True) | published_story_q("story")
            ),
            pk=video_id,
        )

    @staticmethod
    def _payload(video, progress):
        return {
            "video_id": video.id,
            "progress": max(0.0, min(1.0, progress.progress)) if progress else 0.0,
            "position_seconds": max(0.0, progress.position_seconds) if progress else 0.0,
            "duration_seconds": max(0.0, progress.duration_seconds) if progress else 0.0,
            "updated_at": progress.updated_at if progress else None,
        }

    def get(self, request, video_id):
        video = self._get_video(video_id)
        progress = VideoWatchProgress.objects.filter(user=request.user, video=video).first()
        return Response(self._payload(video, progress))

    def put(self, request, video_id):
        video = self._get_video(video_id)
        serializer = VideoProgressWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        progress_obj, _ = VideoWatchProgress.objects.get_or_create(
            user=request.user, video=video, defaults={"story": video.story}
        )
        progress_obj.progress = max(progress_obj.progress, serializer.validated_data["progress"])
        progress_obj.position_seconds = serializer.validated_data["position_seconds"]
        progress_obj.duration_seconds = serializer.validated_data["duration_seconds"]
        progress_obj.save()

        return Response(self._payload(video, progress_obj))


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


class QuickReadProgressAPIView(APIView):
    """Tracks authenticated scroll depth through a Quick Read summary."""

    permission_classes = [IsAuthenticated]

    def get(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        progress = QuickReadProgress.objects.filter(user=request.user, story=story).first()
        if not progress:
            return Response({"detail": "Progress not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(QuickReadProgressSerializer(progress).data)

    def put(self, request, story_slug):
        story = get_object_or_404(Story.objects.published(), slug=story_slug)
        if not (story.summary or "").strip():
            return Response(
                {"detail": "This story has no Quick Read summary."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = QuickReadProgressWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        progress_obj, _ = QuickReadProgress.objects.get_or_create(
            user=request.user, story=story
        )
        progress_obj.progress = max(
            progress_obj.progress, serializer.validated_data["progress"]
        )
        progress_obj.save()
        return Response(QuickReadProgressSerializer(progress_obj).data)


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

        payload = FileReadingProgressSerializer(progress_obj).data
        annotate_completion(payload, record_completion_if_finished(request.user, story))
        return Response(payload)

from django.conf import settings
from django.db import models
import uuid

from apps.story.models import Story, Chapter, Audio, Video, Blog


class ReadingProgress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reading_progress"
    )
    story = models.ForeignKey(
        Story,
        on_delete=models.CASCADE,
        related_name="reading_progress"
    )
    chapter = models.ForeignKey(
        Chapter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    # Intra-chapter progress (0.0 – 1.0)
    progress = models.FloatField(default=0.0)

    # Optional: paragraph or element ID inside CKEditor HTML
    last_element_id = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "story")
        indexes = [
            models.Index(fields=["user", "story"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.story} ({self.progress:.2%})"


class ChapterReadingProgress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chapter_reading_progress",
    )
    story = models.ForeignKey(
        Story,
        on_delete=models.CASCADE,
        related_name="chapter_reading_progress",
    )
    chapter = models.ForeignKey(
        Chapter,
        on_delete=models.CASCADE,
        related_name="chapter_reading_progress",
    )
    progress = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "chapter")
        indexes = [
            models.Index(fields=["user", "story"]),
            models.Index(fields=["user", "chapter"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.chapter} ({self.progress:.2%})"


class FileReadingProgress(models.Model):
    """Progress through a story's EPUB or PDF file — unlike chapters/audios
    there's only ever one file per format per story, so one row per
    (user, story, format) is enough (no per-item breakdown needed)."""

    FORMAT_EPUB = "epub"
    FORMAT_PDF = "pdf"
    FORMAT_CHOICES = [
        (FORMAT_EPUB, "EPUB"),
        (FORMAT_PDF, "PDF"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="file_reading_progress",
    )
    story = models.ForeignKey(
        Story,
        on_delete=models.CASCADE,
        related_name="file_reading_progress",
    )
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES)

    # Overall progress through the file (0.0 - 1.0)
    progress = models.FloatField(default=0.0)

    # Format-specific position marker: an EPUB CFI string for "epub", or a
    # page number (stored as text) for "pdf" — opaque to the backend, just
    # round-tripped so the frontend reader can resume exactly where it left off.
    position = models.CharField(max_length=512, blank=True, null=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "story", "format")
        indexes = [
            models.Index(fields=["user", "story", "format"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.story} [{self.format}] ({self.progress:.2%})"


class BlogReadingProgress(models.Model):
    """Scroll-depth progress through a blog post — mirrors ReadingProgress's
    role for stories, but blogs are single-page (no chapter concept), so
    there's just one progress fraction per (user, blog). Same limitation as
    ReadingProgress: authenticated readers only, so this can't say anything
    about anonymous readers' depth (see AnalyticsEvent's reading_session for
    the anonymous-inclusive open+duration signal instead)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blog_reading_progress",
    )
    blog = models.ForeignKey(
        Blog,
        on_delete=models.CASCADE,
        related_name="reading_progress",
    )
    # Scroll depth through the post (0.0 - 1.0), not time-based.
    progress = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "blog")
        indexes = [
            models.Index(fields=["user", "blog"]),
            models.Index(fields=["blog", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.blog} ({self.progress:.2%})"


class AudioReadingProgress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audio_reading_progress",
    )
    story = models.ForeignKey(
        Story,
        on_delete=models.CASCADE,
        related_name="audio_reading_progress",
    )
    audio = models.ForeignKey(
        Audio,
        on_delete=models.CASCADE,
        related_name="audio_reading_progress",
    )
    progress = models.FloatField(default=0.0)
    position_seconds = models.FloatField(default=0.0)
    duration_seconds = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "audio")
        indexes = [
            models.Index(fields=["user", "story"]),
            models.Index(fields=["user", "audio"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.audio} ({self.progress:.2%})"


class VideoWatchProgress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="video_watch_progress",
    )
    story = models.ForeignKey(
        Story,
        on_delete=models.CASCADE,
        related_name="video_watch_progress",
    )
    video = models.ForeignKey(
        Video,
        on_delete=models.CASCADE,
        related_name="video_watch_progress",
    )
    progress = models.FloatField(default=0.0)
    position_seconds = models.FloatField(default=0.0)
    duration_seconds = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "video")
        indexes = [
            models.Index(fields=["user", "story"]),
            models.Index(fields=["user", "video"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.video} ({self.progress:.2%})"


class AnalyticsEvent(models.Model):
    EVENT_VISIT = "visit"
    EVENT_AD_IMPRESSION = "ad_impression"
    EVENT_READING_SESSION = "reading_session"
    EVENT_LISTENING_SESSION = "listening_session"
    EVENT_WATCHING_SESSION = "watching_session"
    EVENT_COMPLETION = "completion"
    EVENT_DOWNLOAD = "download"
    EVENT_READ_ALONG_CUE_SEEK = "read_along_cue_seek"
    EVENT_READ_ALONG_FOLLOW_TOGGLE = "read_along_follow_toggle"
    EVENT_CHOICES = [
        (EVENT_VISIT, "Visit"),
        (EVENT_AD_IMPRESSION, "Ad impression"),
        (EVENT_READING_SESSION, "Reading session"),
        (EVENT_LISTENING_SESSION, "Listening session"),
        (EVENT_WATCHING_SESSION, "Watching session"),
        (EVENT_COMPLETION, "Completion"),
        (EVENT_DOWNLOAD, "Download"),
        (EVENT_READ_ALONG_CUE_SEEK, "Read Along cue seek"),
        (EVENT_READ_ALONG_FOLLOW_TOGGLE, "Read Along follow toggle"),
    ]

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event_type = models.CharField(max_length=32, choices=EVENT_CHOICES, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
    )
    visitor_id = models.CharField(max_length=64, db_index=True)
    session_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    story = models.ForeignKey(
        Story,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
    )
    blog = models.ForeignKey(
        Blog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
    )
    duration_seconds = models.FloatField(default=0)
    value = models.FloatField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "created_at"], name="stats_event_type_created_idx"),
            models.Index(fields=["visitor_id", "created_at"], name="stats_visitor_created_idx"),
            models.Index(fields=["user", "created_at"], name="stats_user_created_idx"),
            models.Index(fields=["story", "event_type"], name="stats_story_event_idx"),
            models.Index(fields=["blog", "event_type"], name="stats_blog_event_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} - {self.visitor_id}"

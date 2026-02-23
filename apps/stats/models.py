from django.conf import settings
from django.db import models

from apps.story.models import Story, Chapter, Audio


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

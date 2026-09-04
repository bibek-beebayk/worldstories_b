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
    # The Quick Read funnel (§2.3 / §12.2). Deliberately their own event types
    # rather than metadata on `completion`: a summary read is not a story
    # finished, and folding it into `completion` would inflate a story's
    # completion count with readers who only ever read the summary. The
    # conversion metric is a ratio of these three, so they need to be countable
    # on their own.
    EVENT_QUICK_READ_OPENED = "quick_read_opened"
    EVENT_QUICK_READ_COMPLETED = "quick_read_completed"
    EVENT_QUICK_READ_FULL_STORY_CLICKED = "quick_read_full_story_clicked"
    # The reading lifecycle (§2.5 / §12.2): start rate is starts over story
    # detail views, completion rate is completions over starts. Named in the
    # past tense throughout — the source document writes "story_resume" in §2.5
    # and "story_resumed" in §12.1; the latter is used here so the whole set
    # reads consistently as things that happened.
    EVENT_STORY_STARTED = "story_started"
    EVENT_STORY_RESUMED = "story_resumed"
    EVENT_STORY_PROGRESSED = "story_progressed"
    EVENT_STORY_COMPLETED = "story_completed"
    EVENT_NEXT_STORY_CLICKED = "next_story_clicked"
    # Story Passport (§5). country_unlocked is raised by the server beside the
    # completion that caused it, so it is exactly-once per reader per country.
    EVENT_COUNTRY_UNLOCKED = "country_unlocked"
    EVENT_PASSPORT_VIEWED = "passport_viewed"
    # Raised by the server beside the award itself (apps/stats/achievements.py),
    # so it inherits the conditional update that makes awarding once-only.
    EVENT_ACHIEVEMENT_UNLOCKED = "achievement_unlocked"
    # Story Journeys. Both are server-raised beside the completion that caused
    # them, and both are decided from completion counts — a journey passes
    # through 1-of-N and N-of-N exactly once per reader, so neither needs extra
    # state to be once-only.
    EVENT_JOURNEY_STARTED = "journey_started"
    EVENT_JOURNEY_COMPLETED = "journey_completed"
    # An interaction rather than a milestone: unlike the completion-derived
    # events above, this legitimately fires again when a reader changes their
    # mind, carrying what it replaced.
    EVENT_REACTION_ADDED = "reaction_added"
    EVENT_DAILY_STORY_VIEWED = "daily_story_viewed"
    EVENT_DAILY_STORY_STARTED = "daily_story_started"
    EVENT_DAILY_STORY_COMPLETED = "daily_story_completed"
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
        (EVENT_QUICK_READ_OPENED, "Quick Read opened"),
        (EVENT_QUICK_READ_COMPLETED, "Quick Read completed"),
        (EVENT_QUICK_READ_FULL_STORY_CLICKED, "Quick Read full story clicked"),
        (EVENT_STORY_STARTED, "Story started"),
        (EVENT_STORY_RESUMED, "Story resumed"),
        (EVENT_STORY_PROGRESSED, "Story progressed"),
        (EVENT_STORY_COMPLETED, "Story completed"),
        (EVENT_NEXT_STORY_CLICKED, "Next story clicked"),
        (EVENT_COUNTRY_UNLOCKED, "Country unlocked"),
        (EVENT_PASSPORT_VIEWED, "Passport viewed"),
        (EVENT_ACHIEVEMENT_UNLOCKED, "Achievement unlocked"),
        (EVENT_JOURNEY_STARTED, "Journey started"),
        (EVENT_JOURNEY_COMPLETED, "Journey completed"),
        (EVENT_REACTION_ADDED, "Reaction added"),
        (EVENT_DAILY_STORY_VIEWED, "Daily Story viewed"),
        (EVENT_DAILY_STORY_STARTED, "Daily Story started"),
        (EVENT_DAILY_STORY_COMPLETED, "Daily Story completed"),
    ]

    # visitor_id for an event the server raises itself rather than receiving
    # from a browser. Such events are always attributed to a real user, and
    # every aggregation keys on the user when one is present
    # (see _content_identity in analytics_api.py), so this is never read.
    SERVER_VISITOR_ID = "server"

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


class StoryCompletion(models.Model):
    """"This user finished this story", as a durable fact.

    Completion used to live in two places, neither of which could answer that
    question. One was an append-only ``AnalyticsEvent`` whose idempotency came
    from a *localStorage* key, so clearing site data or opening the story on a
    second device recorded the same finish again. The other was a query that
    recomputed completion on the fly by averaging ``ChapterReadingProgress``
    over the story's chapter count — which meant an audiobook, a video, an
    EPUB or a PDF story could never be complete at all, because none of them
    have chapters.

    The Story Passport, achievements, journeys and the weekly recap all need
    to ask "which stories, and which countries, has this reader finished, and
    when" — a query, with timestamps, that survives a device change. Hence a
    row rather than a derivation: ``completed_at`` is a fact about the reader's
    history that no amount of recomputation from current progress can
    reconstruct, and first-unlock events depend on knowing it happened *now*
    rather than at some point in the past.

    One row per (user, story), enforced by the database. Finishing the same
    story again — re-reading it, or finishing the audiobook after the text —
    updates nothing and creates nothing; ``source`` records how it was first
    completed.
    """

    SOURCE_CHAPTERS = "chapters"
    SOURCE_AUDIO = "audio"
    SOURCE_VIDEO = "video"
    SOURCE_EPUB = "epub"
    SOURCE_PDF = "pdf"
    SOURCE_BACKFILL = "backfill"
    SOURCE_CHOICES = [
        (SOURCE_CHAPTERS, "Chapters"),
        (SOURCE_AUDIO, "Audio"),
        (SOURCE_VIDEO, "Video"),
        (SOURCE_EPUB, "EPUB"),
        (SOURCE_PDF, "PDF"),
        (SOURCE_BACKFILL, "Backfilled from historical progress"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="story_completions",
    )
    story = models.ForeignKey(
        Story,
        on_delete=models.CASCADE,
        related_name="completions",
    )
    # Which surface the reader actually finished it on. Not a carve-out — a
    # story finished as an audiobook is as completed as one finished as text;
    # this only records which way round it happened.
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "story")
        indexes = [
            # "Everything this reader has finished, newest first" — the
            # profile's Completed list and the weekly recap.
            models.Index(fields=["user", "-completed_at"], name="stats_completion_user_idx"),
            # "Who has finished this story" — story-level reporting.
            models.Index(fields=["story"], name="stats_completion_story_idx"),
        ]
        ordering = ["-completed_at"]

    def __str__(self):
        return f"{self.user} completed {self.story} ({self.source})"


class Achievement(models.Model):
    """A goal a reader can reach, defined as data rather than code.

    Everything about an achievement — what it measures, how far, whether it is
    shown before it is earned — lives in a row, so the catalogue can grow
    without a deploy. `target_type` names the measure and `target_value` the
    threshold, which is what lets one evaluator serve every achievement
    (see apps/stats/achievements.py) rather than a branch per badge.

    Deliberately *not* points, coins or a currency: the source document rules
    out generic gamification, and every target below is a count of real reading.
    """

    CATEGORY_READING = "reading"
    CATEGORY_COUNTRIES = "countries"
    CATEGORY_GENRE = "genre"
    CATEGORY_STREAK = "streak"
    CATEGORY_QUICK_READ = "quick_read"
    CATEGORY_JOURNEY = "journey"
    CATEGORY_CHOICES = [
        (CATEGORY_READING, "Reading"),
        (CATEGORY_COUNTRIES, "Countries"),
        (CATEGORY_GENRE, "Genre"),
        (CATEGORY_STREAK, "Streak"),
        (CATEGORY_QUICK_READ, "Quick Read"),
        (CATEGORY_JOURNEY, "Journeys"),
    ]

    # What the achievement counts. Each maps to one measure function in
    # apps/stats/achievements.py; adding a type means adding that function.
    TARGET_STORIES_COMPLETED = "stories_completed"
    TARGET_COUNTRIES_EXPLORED = "countries_explored"
    TARGET_GENRE_COMPLETED = "genre_completed"
    TARGET_STREAK_DAYS = "streak_days"
    TARGET_QUICK_READS_COMPLETED = "quick_reads_completed"
    TARGET_JOURNEYS_COMPLETED = "journeys_completed"
    TARGET_TYPE_CHOICES = [
        (TARGET_STORIES_COMPLETED, "Stories completed"),
        (TARGET_COUNTRIES_EXPLORED, "Countries explored"),
        (TARGET_GENRE_COMPLETED, "Stories completed in a genre"),
        (TARGET_STREAK_DAYS, "Longest reading streak, in days"),
        (TARGET_QUICK_READS_COMPLETED, "Quick Reads completed"),
        (TARGET_JOURNEYS_COMPLETED, "Story Journeys completed"),
    ]

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    description = models.CharField(max_length=280)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    # An emoji rather than an asset: a badge set is a design project, and this
    # keeps the catalogue editable without one.
    icon = models.CharField(max_length=8, blank=True)
    target_type = models.CharField(max_length=32, choices=TARGET_TYPE_CHOICES, db_index=True)
    target_value = models.PositiveIntegerField()
    # Which genre a genre achievement counts, as a slug. A plain field rather
    # than an FK because it is meaningless for every other target type, and an
    # always-null FK on most rows would invite exactly the wrong queries.
    target_key = models.CharField(max_length=140, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    # Hidden achievements are not listed until earned. None are hidden today;
    # the flag exists so a surprise can be added without a schema change.
    hidden = models.BooleanField(default=False)
    # Display order within a category — "10 stories" should sit above "25"
    # regardless of name or creation order.
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["category", "order", "target_value"]
        indexes = [
            models.Index(fields=["active", "target_type"], name="stats_achievement_active_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.target_type} >= {self.target_value})"


class UserAchievement(models.Model):
    """One reader's progress toward one achievement.

    A row exists as soon as there is progress worth recording, so the profile
    can show "7 of 10" rather than only a binary earned/not. `completed_at` is
    set exactly once, by the conditional update in
    apps/stats/achievements.py::award — never by re-saving this model, which is
    what keeps a re-run of a trigger from re-awarding.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="achievements",
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name="user_achievements",
    )
    progress = models.PositiveIntegerField(default=0)
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "achievement")
        indexes = [
            models.Index(fields=["user", "completed"], name="stats_userach_user_idx"),
        ]
        ordering = ["-completed_at", "achievement__order"]

    def __str__(self):
        state = "completed" if self.completed else f"{self.progress}/{self.achievement.target_value}"
        return f"{self.user} — {self.achievement.name} ({state})"

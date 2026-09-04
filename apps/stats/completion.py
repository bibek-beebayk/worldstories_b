"""Decides when a reader has finished a story, and records it.

Called from every progress-write endpoint, so completion is settled by the
server on the same request that moves the progress — not by the client, and
not by a query that recomputes it later. That is what makes it idempotent
across devices and immune to cleared browser storage: the uniqueness
constraint on ``StoryCompletion`` is the whole mechanism.

The rule per surface is "every item of that surface is finished":

    chapters   every Chapter has ChapterReadingProgress >= THRESHOLD
    audio      every Audio has AudioReadingProgress >= THRESHOLD
    video      every Video has VideoWatchProgress >= THRESHOLD
    epub/pdf   that format's FileReadingProgress >= THRESHOLD

A story completes when *any* one of its surfaces does. Finishing the audiobook
is finishing the story; a reader who listens rather than reads has not read
half a story. This is the fix for the old derived query, which only ever
looked at chapters and so could never complete an audio-, video- or file-only
story at all.

Quick Read is deliberately not a surface here. Per the requirements document
(§5.2), reading a summary does not complete the story and must not unlock a
country in the Story Passport.
"""

from apps.story.models import Audio, Chapter, Video

# Matches the client-side threshold in progressSync.ts. A reader who reaches
# the last line rarely scrolls to a mathematically exact 1.0 — the final
# fraction of a percent is scrollbar rounding, not unread text.
COMPLETION_THRESHOLD = 0.995


def _all_items_finished(item_ids, finished_ids):
    """True when there is at least one item and every one of them is done.

    The emptiness check matters: without it ``all()`` over an empty set is
    vacuously true, and every story would instantly complete on every surface
    it doesn't have.
    """
    return bool(item_ids) and item_ids <= finished_ids


def completed_source(user, story):
    """Which surface, if any, this user has finished this story on.

    Returns a ``StoryCompletion.SOURCE_*`` value, or None. Where more than one
    surface is finished, the first match wins; the value is only a record of
    how it happened, so the order is a tie-break rather than a ranking.
    """
    # Imported here rather than at module import time: stats.models imports
    # from story.models, and this module is imported from stats.views.
    from apps.stats.models import (
        AudioReadingProgress,
        ChapterReadingProgress,
        FileReadingProgress,
        StoryCompletion,
        VideoWatchProgress,
    )

    def finished(model, field, item_model):
        item_ids = set(item_model.objects.filter(story=story).values_list("id", flat=True))
        finished_ids = set(
            model.objects.filter(
                user=user, story=story, progress__gte=COMPLETION_THRESHOLD
            ).values_list(field, flat=True)
        )
        return _all_items_finished(item_ids, finished_ids)

    if finished(ChapterReadingProgress, "chapter_id", Chapter):
        return StoryCompletion.SOURCE_CHAPTERS
    if finished(AudioReadingProgress, "audio_id", Audio):
        return StoryCompletion.SOURCE_AUDIO
    if finished(VideoWatchProgress, "video_id", Video):
        return StoryCompletion.SOURCE_VIDEO

    file_formats = set(
        FileReadingProgress.objects.filter(
            user=user, story=story, progress__gte=COMPLETION_THRESHOLD
        ).values_list("format", flat=True)
    )
    # Only counts if the story actually has the file the reader finished —
    # a stale progress row for a format since removed shouldn't complete it.
    if FileReadingProgress.FORMAT_EPUB in file_formats and story.epub_file:
        return StoryCompletion.SOURCE_EPUB
    if FileReadingProgress.FORMAT_PDF in file_formats and story.pdf_file:
        return StoryCompletion.SOURCE_PDF

    return None


def record_completion_if_finished(user, story):
    """Record the completion if this progress write finished the story.

    Returns the ``StoryCompletion`` when this call is what created it, and None
    otherwise — including when the story was already complete. Callers use that
    "newly completed *now*" signal for the things that must fire exactly once:
    the completion screen, a country's first unlock, an achievement.

    Safe to call on every progress write. ``get_or_create`` against the
    (user, story) uniqueness constraint absorbs concurrent writes from two
    devices finishing the same story at the same moment.
    """
    from apps.stats.models import AnalyticsEvent, StoryCompletion

    if not user or not user.is_authenticated:
        return None

    source = completed_source(user, story)
    if source is None:
        return None

    completion, created = StoryCompletion.objects.get_or_create(
        user=user, story=story, defaults={"source": source}
    )
    if not created:
        return None

    # Raised here rather than from the browser, so the analytics stream inherits
    # the uniqueness constraint above: exactly one story_completed per reader
    # per story, no matter how many devices they read on or how often they
    # clear their browser storage. The client-side `completion` event this
    # replaces deduplicated on a localStorage key, which a second device does
    # not have.
    AnalyticsEvent.objects.create(
        event_type=AnalyticsEvent.EVENT_STORY_COMPLETED,
        user=user,
        story=story,
        visitor_id=AnalyticsEvent.SERVER_VISITOR_ID,
        metadata={"source": source},
    )

    # Daily Story dates use the site's explicit UTC timezone. This event is
    # server-owned like story_completed: it can only fire on the unique write
    # that records completion, never once per browser or device.
    from django.utils import timezone
    from apps.story.models import DailyStory

    if DailyStory.objects.filter(date=timezone.localdate(), story=story, active=True).exists():
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_DAILY_STORY_COMPLETED,
            user=user,
            story=story,
            visitor_id=AnalyticsEvent.SERVER_VISITOR_ID,
            metadata={"date": timezone.localdate().isoformat(), "source": source},
        )

    # A country is unlocked by the completion that first reaches it, so this
    # belongs here rather than anywhere the Passport is read. Same guarantee as
    # the completion itself: raised once, on the write that caused it.
    from apps.stats.passport import newly_unlocked_country

    unlocked = newly_unlocked_country(user, story)
    if unlocked:
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_COUNTRY_UNLOCKED,
            user=user,
            story=story,
            visitor_id=AnalyticsEvent.SERVER_VISITOR_ID,
            metadata={"country": unlocked},
        )
        # Carried on the object so the progress endpoints can hand the reader a
        # toast without a second request. Not a model field — it is true of
        # this response, not of the row.
        completion.unlocked_country = unlocked

    # Evaluated here rather than anywhere achievements are read: §6.3 forbids
    # recalculating on page view, and this is the write that could have moved
    # a reading, genre, country or streak counter.
    from apps.stats.achievements import evaluate

    completion.unlocked_achievements = evaluate(user, "story_completed")

    return completion

"""Session-time capping and per-type content scoring for the admin analytics.

Kept free of any import from analytics_api so that module (and the Overview)
can use it without a cycle.
"""
from django.db.models import Sum

from apps.stats.models import AnalyticsEvent

from .models import Audio, Story, Video

# --- Session caps -----------------------------------------------------------
#
# A session event is "time the tab was visible", so a tab left open overnight is
# a single enormous session. Every minute total counts each session only up to
# a ceiling that depends on what was being consumed.
SESSION_CAP_FACTOR = 1.5  # people re-read, pause and seek: allow 1.5x the content's length
SESSION_FALLBACK_CAP_SECONDS = 60 * 60  # when the content's length isn't known
SESSION_CAP_FLOOR_SECONDS = 10 * 60  # never cap a session below this (short items get re-read)
SESSION_CAP_CEILING_SECONDS = 3 * 60 * 60  # nor above this (a 20h audiobook isn't one sitting)
QUICK_READ_CAP_SECONDS = SESSION_CAP_FLOOR_SECONDS  # a summary is a couple of minutes long

SESSION_EVENT_TYPES = (
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
)


class SessionCaps:
    """The one place a session's counted duration is decided.

    Preloads each story's estimated reading time, audio length and each
    video's length once, then `capped()` is a dictionary lookup per event.

    - reading a story: its estimated reading minutes (chapters, else the
      EPUB/PDF estimate) x 1.5
    - listening to a story: the total length of its audio tracks x 1.5
    - watching a video: that video's duration x 1.5
    - a Quick Read: a fixed 10 minutes; a blog post, or anything whose length
      is unknown: a fixed 60 minutes
    The result is always kept within [10 min, 3 h].
    """

    def __init__(self):
        self.reading_seconds = {}
        for story_id, chapter_minutes, file_minutes in Story.objects.values_list(
            "id", "cached_chapter_reading_minutes", "cached_file_reading_minutes"
        ):
            minutes = chapter_minutes or file_minutes
            if minutes:
                self.reading_seconds[story_id] = minutes * 60
        self.audio_seconds = {
            story_id: total
            for story_id, total in Audio.objects.filter(duration_seconds__gt=0)
            .values_list("story_id")
            .annotate(total=Sum("duration_seconds"))
            if total
        }
        self.video_seconds = dict(
            Video.objects.filter(duration_seconds__gt=0).values_list("id", "duration_seconds")
        )

    def cap_seconds(self, event_type, *, story_id=None, blog_id=None, video_id=None, is_quick_read=False):
        if is_quick_read:
            return QUICK_READ_CAP_SECONDS
        known = None
        if event_type == AnalyticsEvent.EVENT_READING_SESSION and not blog_id:
            known = self.reading_seconds.get(story_id)
        elif event_type == AnalyticsEvent.EVENT_LISTENING_SESSION:
            known = self.audio_seconds.get(story_id)
        elif event_type == AnalyticsEvent.EVENT_WATCHING_SESSION:
            known = self.video_seconds.get(video_id)
        if known is None:
            return SESSION_FALLBACK_CAP_SECONDS
        return min(SESSION_CAP_CEILING_SECONDS, max(SESSION_CAP_FLOOR_SECONDS, known * SESSION_CAP_FACTOR))

    def capped(self, seconds, event_type, **kwargs):
        """`seconds`, limited to the ceiling for this kind of session."""
        return min(seconds or 0, self.cap_seconds(event_type, **kwargs))


# --- Scoring ----------------------------------------------------------------

SCORE_WEIGHTS = {"people": 0.35, "minutes": 0.25, "completion": 0.25, "actions": 0.15}
# A completion rate over a handful of signed-in readers is noise; it counts in
# full only once this many people back it.
COMPLETION_CONFIDENCE_PEOPLE = 5
# Fewer unique people than this and a title's score says very little.
LOW_SAMPLE_PEOPLE = 2

SCORE_FORMULA = {
    "range": [0, 100],
    "weights": SCORE_WEIGHTS,
    "completion_confidence_people": COMPLETION_CONFIDENCE_PEOPLE,
    "low_sample_people": LOW_SAMPLE_PEOPLE,
    "summary": (
        "Score (0-100) = 35% unique people + 25% time spent + 25% completion + 15% favorites, "
        "reviews and reactions. People, time and actions are measured against the best title of "
        "the same type in this range; completion is the share of signed-in readers who finished, "
        "counted in full once 5 people back it. Time counts each session only up to a cap "
        "(1.5x the content's length, at most 3 hours). Audiobooks are scored on listening only. "
        "Titles with no people score 0."
    ),
}


def score_rows(rows):
    """Add `performance_score` (0-100) and `low_sample` to ranking rows, in place.

    Rows must carry unique_people, engagement_minutes, completion_rate,
    tracked_people and actions. Normalisation is against the best row of the
    same set, so score rows of one content type at a time.

        score = 100 x ( 0.35 x people/max_people
                      + 0.25 x minutes/max_minutes
                      + 0.25 x completion_rate x min(1, tracked_people / 5)
                      + 0.15 x actions/max_actions )

    A title with no unique people scores 0 whatever else it has: an audiobook
    nobody listened to must not rank on a favorite alone.
    """
    max_people = max((row["unique_people"] for row in rows), default=0)
    max_minutes = max((row["engagement_minutes"] for row in rows), default=0)
    max_actions = max((row["actions"] for row in rows), default=0)
    for row in rows:
        if not row["unique_people"]:
            score = 0.0
        else:
            confidence = min(1, row["tracked_people"] / COMPLETION_CONFIDENCE_PEOPLE)
            score = 100 * (
                SCORE_WEIGHTS["people"] * row["unique_people"] / max_people
                + SCORE_WEIGHTS["minutes"] * (row["engagement_minutes"] / max_minutes if max_minutes else 0)
                + SCORE_WEIGHTS["completion"] * (row["completion_rate"] or 0) * confidence
                + SCORE_WEIGHTS["actions"] * (row["actions"] / max_actions if max_actions else 0)
            )
        row["performance_score"] = round(score, 1)
        row["low_sample"] = row["unique_people"] < LOW_SAMPLE_PEOPLE
    return rows

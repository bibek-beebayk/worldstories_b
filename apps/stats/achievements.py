"""Awarding achievements, incrementally.

§6.3 is explicit that achievement progress must not be recalculated on every
page view, and the shape of this module is what enforces that: nothing here is
called from a read path. `evaluate` takes the *target types* an event could
possibly have moved, and only those are measured — finishing a story never
touches the Quick Read counters, and viewing a profile touches nothing at all.

Awarding follows the same principle as `StoryCompletion` and `country_unlocked`
before it: the event is raised beside the write that caused it, and the
"newly earned" transition is a conditional UPDATE rather than a read-then-write.
Two devices finishing a reader's tenth story at the same moment therefore
produce one award and one event, not two.
"""

from django.db import transaction
from django.utils import timezone

# Which target types each trigger can possibly have moved. Kept as data so a
# caller states *what happened* rather than guessing what to recompute — the
# thing that goes wrong later is someone adding an achievement type and
# forgetting one of the branches that should now recompute it.
TRIGGER_TARGETS = {
    "story_completed": (
        "stories_completed",
        "genre_completed",
        "countries_explored",
        "streak_days",
        "journeys_completed",
    ),
    "quick_read_completed": ("quick_reads_completed",),
    "journey_completed": ("journeys_completed",),
    "country_unlocked": ("countries_explored",),
    "streak_changed": ("streak_days",),
}


def _stories_completed(user, _key):
    from apps.stats.models import StoryCompletion

    return StoryCompletion.objects.filter(user=user).count()


def _countries_explored(user, _key):
    from apps.stats.passport import explored_countries

    return len(explored_countries(user))


def _genre_completed(user, genre_slug):
    from apps.stats.models import StoryCompletion

    if not genre_slug:
        return 0
    return StoryCompletion.objects.filter(
        user=user, story__genres__slug=genre_slug
    ).distinct().count()


def _streak_days(user, _key):
    from apps.stats.models import AnalyticsEvent
    from apps.stats.streaks import compute_streak

    created_ats = AnalyticsEvent.objects.filter(
        user=user,
        event_type__in=[
            AnalyticsEvent.EVENT_READING_SESSION,
            AnalyticsEvent.EVENT_LISTENING_SESSION,
            AnalyticsEvent.EVENT_WATCHING_SESSION,
        ],
    ).values_list("created_at", flat=True)
    activity_dates = {timezone.localtime(value).date() for value in created_ats}
    # The longest run, not the current one: a streak achievement earned in
    # March should not be taken away in April.
    _current, longest = compute_streak(activity_dates, timezone.localdate())
    return longest


def _quick_reads_completed(user, _key):
    from apps.stats.models import AnalyticsEvent

    # Distinct stories rather than events: re-reading the same summary twice is
    # not two Quick Reads. The funnel event fires once per visit by design
    # (see useQuickReadFunnel), so without this a single summary revisited
    # would inflate the count.
    return (
        AnalyticsEvent.objects.filter(
            user=user,
            event_type=AnalyticsEvent.EVENT_QUICK_READ_COMPLETED,
            story__isnull=False,
        )
        .values("story_id")
        .distinct()
        .count()
    )


def _journeys_completed(user, _key):
    from apps.stats.journeys import journeys_for_reader

    return sum(1 for row in journeys_for_reader(user) if row["is_complete"])


MEASURES = {
    "stories_completed": _stories_completed,
    "countries_explored": _countries_explored,
    "genre_completed": _genre_completed,
    "streak_days": _streak_days,
    "quick_reads_completed": _quick_reads_completed,
    "journeys_completed": _journeys_completed,
}


def award(user, achievement, progress):
    """Record progress, and return the achievement if this call earned it.

    The completion flip is a conditional UPDATE filtered on `completed=False`,
    so only one caller can ever win it. Returning None for an achievement that
    was already earned is what makes every trigger safe to re-run.
    """
    from apps.stats.models import UserAchievement

    reached = progress >= achievement.target_value
    row, _created = UserAchievement.objects.get_or_create(
        user=user, achievement=achievement, defaults={"progress": progress}
    )

    # Progress only moves forward. A story unpublished after being finished
    # would otherwise walk a reader's counter backwards.
    if progress > row.progress:
        UserAchievement.objects.filter(pk=row.pk).update(progress=progress)

    if not reached or row.completed:
        return None

    earned = UserAchievement.objects.filter(pk=row.pk, completed=False).update(
        completed=True, completed_at=timezone.now(), progress=max(progress, row.progress)
    )
    return achievement if earned == 1 else None


def evaluate(user, trigger):
    """Re-measure only what `trigger` could have moved, and award what is due.

    Returns the achievements earned *by this call* — the list a response can
    turn into a notification, and empty on every re-run.
    """
    from apps.stats.models import Achievement, AnalyticsEvent

    if not user or not user.is_authenticated:
        return []

    target_types = TRIGGER_TARGETS.get(trigger)
    if not target_types:
        return []

    achievements = Achievement.objects.filter(active=True, target_type__in=target_types)
    if not achievements:
        return []

    # One measurement per (target_type, target_key) pair, not one per
    # achievement: the five "stories completed" tiers share a single count.
    measured = {}
    earned = []
    for achievement in achievements:
        key = (achievement.target_type, achievement.target_key)
        if key not in measured:
            measure = MEASURES.get(achievement.target_type)
            measured[key] = measure(user, achievement.target_key) if measure else 0

        with transaction.atomic():
            unlocked = award(user, achievement, measured[key])
        if unlocked:
            earned.append(unlocked)

    for achievement in earned:
        AnalyticsEvent.objects.create(
            event_type=AnalyticsEvent.EVENT_ACHIEVEMENT_UNLOCKED,
            user=user,
            visitor_id=AnalyticsEvent.SERVER_VISITOR_ID,
            metadata={
                "achievement": achievement.slug,
                "category": achievement.category,
                "trigger": trigger,
            },
        )

    return earned


def serialize_earned(achievements):
    """The minimum a client needs to show an unlock notification."""
    return [
        {
            "slug": achievement.slug,
            "name": achievement.name,
            "description": achievement.description,
            "icon": achievement.icon,
            "category": achievement.category,
        }
        for achievement in achievements
    ]

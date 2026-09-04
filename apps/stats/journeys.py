"""Reader progress through a Story Journey.

Derived from `StoryCompletion`, never stored — the same decision as the Story
Passport, for the same reason: a second table would have to be kept in step
with completions forever, and every drift would be a reader's journey progress
disagreeing with their own completed list.

Completion is judged on the journey's **required** items only, so an editor can
add a bonus story without moving the finish line for readers already part-way
through.
"""

from apps.story.models import StoryJourney


def _completed_story_ids(user):
    from apps.stats.models import StoryCompletion

    if not user or not user.is_authenticated:
        return set()
    return set(
        StoryCompletion.objects.filter(user=user).values_list("story_id", flat=True)
    )


def journey_progress(journey, completed_ids):
    """`(completed_required, total_required, is_complete)` for one journey.

    A journey with no required items is never complete: an empty journey is
    unfinished, not finished — the same vacuous-truth trap the completion rule
    guards against, where `all()` over an empty set would award it to everyone.
    """
    required_ids = [
        item.story_id for item in journey.items.all() if item.required
    ]
    if not required_ids:
        return 0, 0, False
    done = sum(1 for story_id in required_ids if story_id in completed_ids)
    return done, len(required_ids), done == len(required_ids)


def journeys_for_reader(user, journeys=None):
    """Every active journey, with this reader's progress through each.

    One completions query for the whole page regardless of how many journeys
    there are.
    """
    if journeys is None:
        journeys = (
            StoryJourney.objects.filter(active=True)
            .prefetch_related("items")
            .order_by("order", "title")
        )
    completed_ids = _completed_story_ids(user)

    rows = []
    for journey in journeys:
        done, total, is_complete = journey_progress(journey, completed_ids)
        # A journey with nothing in it is an editor's work in progress, not a
        # reader-facing thing.
        if total == 0:
            continue
        rows.append(
            {
                "journey": journey,
                "completed": done,
                "total": total,
                "is_complete": is_complete,
            }
        )
    return rows


def journeys_touched_by(story):
    """The active journeys this story belongs to.

    Scopes the work done when a story is finished: only these journeys can have
    changed, so only these are re-checked (the same incremental principle as
    the achievement triggers).
    """
    return list(
        StoryJourney.objects.filter(active=True, items__story=story)
        .prefetch_related("items")
        .distinct()
    )


def journey_events_for_completion(user, story, completed_ids):
    """Which journeys this completion started or finished.

    Returns `(started, completed)` as lists of journeys.

    "Started" is the completion that first gives a reader progress in a journey
    — there is no enrolment step, so the first story finished *is* the start.
    Both are decided from the completion counts, which makes them exactly-once
    without any extra state: a journey can only pass through 1-of-N and N-of-N
    once per reader.
    """
    started = []
    finished = []
    for journey in journeys_touched_by(story):
        done, total, is_complete = journey_progress(journey, completed_ids)
        if total == 0:
            continue
        if done == 1:
            started.append(journey)
        if is_complete:
            finished.append(journey)
    return started, finished

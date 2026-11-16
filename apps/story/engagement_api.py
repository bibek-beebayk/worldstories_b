"""Analytics -> Engagement tab: how readers, listeners and watchers move
through content, computed from audience-filtered AnalyticsEvent rows so that
anonymous readers count (the progress tables only ever hold signed-in users).

One scan of the lifecycle and session events builds a per-(person, story) record
that every section reads. A person is the signed-in user, else the browser's
visitor_id (see _content_identity), so someone on two devices counts once.

What is and isn't observable, because it shapes every number here:
- story_started / story_resumed / story_progressed are emitted by the browser
  for everyone (apps in useStoryReadingEvents: chapter reader, EPUB, PDF and the
  audiobook player). Progress milestones are only 25 / 50 / 75%.
- story_completed is raised by the server and only exists for signed-in users
  (apps/stats/completion.py), so completion is a share of signed-in readers.
- The read-along and video pages emit sessions but no start/progress events.
  Video 50% / completion therefore comes from VideoWatchProgress (signed-in).
"""
from datetime import timedelta

from django.db.models import Avg, Count
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page

from apps.stats.completion import COMPLETION_THRESHOLD
from apps.stats.models import (
    AnalyticsEvent,
    ChapterReadingProgress,
    VideoWatchProgress,
)

from .analytics_api import (
    CACHE_SECONDS,
    _content_identity,
    _retention,
    audience_events,
    audience_only,
    build_with_previous_period,
    fill_time_buckets,
    filter_since,
    get_cutoff,
    get_range_days,
    get_time_interval,
    time_trunc,
)
from .api import IsSuperUser
from .content_scoring import SessionCaps
from .models import Chapter, Favorite, Review, Story, StoryReaction, StoryView

# A rate over fewer people than this says nothing, so it is reported as null.
MIN_SAMPLE = 5
LISTEN_MIN_SECONDS = 30  # "listened" in the listen/watch funnels
DROP_OFF_MIN_STARTERS = 5
DROP_OFF_LIMIT = 10
RATING_DISTRIBUTION_MIN_REVIEWS = 5

# Story length buckets, by estimated reading minutes (a word-count estimate),
# else by chapter count for stories with no estimate yet.
SHORT_MAX_MINUTES = 20
MEDIUM_MAX_MINUTES = 60
MEDIUM_MAX_CHAPTERS = 6
LENGTH_BUCKETS = ("short", "medium", "long")

DEPTH_POINTS = (0, 25, 50, 75, 100)
# Where a reader's furthest milestone says they stopped.
STOP_STAGES = ("0-25%", "25-50%", "50-75%", "75-100%")

LIFECYCLE_TYPES = (
    AnalyticsEvent.EVENT_STORY_STARTED,
    AnalyticsEvent.EVENT_STORY_RESUMED,
    AnalyticsEvent.EVENT_STORY_PROGRESSED,
    AnalyticsEvent.EVENT_STORY_COMPLETED,
)
SESSION_TYPES = (
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
)


class StoryLengths:
    """short / medium / long for each story."""

    def __init__(self):
        self.minutes = {}
        for story_id, chapter_minutes, file_minutes in Story.objects.values_list(
            "id", "cached_chapter_reading_minutes", "cached_file_reading_minutes"
        ):
            if chapter_minutes or file_minutes:
                self.minutes[story_id] = chapter_minutes or file_minutes
        self.chapters = dict(Chapter.objects.values_list("story_id").annotate(n=Count("id")))

    def bucket(self, story_id):
        minutes = self.minutes.get(story_id)
        if minutes:
            if minutes < SHORT_MAX_MINUTES:
                return "short"
            return "medium" if minutes <= MEDIUM_MAX_MINUTES else "long"
        chapters = self.chapters.get(story_id)
        if chapters:
            if chapters <= 1:
                return "short"
            return "medium" if chapters <= MEDIUM_MAX_CHAPTERS else "long"
        return None


def _group_of_format(fmt):
    return "listen" if fmt == "audio" else "read"


def _blank_group():
    return {"opened": False, "fresh": False, "depth": 0.0, "completed": False, "seconds": 0.0}


def _reached(group, fraction):
    return group["completed"] or group["depth"] >= fraction


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _scan_events(days, caps):
    """One pass over the lifecycle and session events in the window."""
    cutoff = get_cutoff(days)
    rows = audience_events(
        filter_since(AnalyticsEvent.objects.filter(event_type__in=LIFECYCLE_TYPES + SESSION_TYPES), cutoff)
    ).values_list(
        "event_type", "user_id", "visitor_id", "story_id", "video_id", "duration_seconds", "value", "metadata", "created_at"
    )

    pairs = {}  # (identity, story_id) -> {"signed_in", "read", "listen"}
    watch = {}  # (identity, video_id) -> capped seconds
    active_days = {}  # identity -> {date}
    session_seconds = {}  # identity -> capped seconds across every session
    for event_type, user_id, visitor_id, story_id, video_id, duration, value, metadata, created_at in rows:
        identity = _content_identity(user_id, visitor_id)
        metadata = metadata or {}
        day = timezone.localtime(created_at).date()

        if event_type in SESSION_TYPES:
            quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and metadata.get("format") == "quick_read"
            seconds = caps.capped(duration, event_type, story_id=story_id, video_id=video_id, is_quick_read=quick)
            active_days.setdefault(identity, set()).add(day)
            session_seconds[identity] = session_seconds.get(identity, 0) + seconds
            if event_type == AnalyticsEvent.EVENT_WATCHING_SESSION:
                if video_id:
                    watch[(identity, video_id)] = watch.get((identity, video_id), 0) + seconds
            elif story_id and not quick:
                pair = pairs.setdefault(
                    (identity, story_id), {"signed_in": bool(user_id), "read": _blank_group(), "listen": _blank_group()}
                )
                group = "listen" if event_type == AnalyticsEvent.EVENT_LISTENING_SESSION else "read"
                pair[group]["seconds"] += seconds
            continue

        if not story_id:
            continue
        pair = pairs.setdefault(
            (identity, story_id), {"signed_in": bool(user_id), "read": _blank_group(), "listen": _blank_group()}
        )
        if event_type == AnalyticsEvent.EVENT_STORY_COMPLETED:
            source = metadata.get("source")
            if source == "video":
                continue  # watched, not read or listened; tracked from VideoWatchProgress
            group = pair["listen" if source == "audio" else "read"]
            group["completed"] = True
            group["depth"] = 1.0
            continue
        group = pair[_group_of_format(metadata.get("format"))]
        active_days.setdefault(identity, set()).add(day)
        if event_type in (AnalyticsEvent.EVENT_STORY_STARTED, AnalyticsEvent.EVENT_STORY_RESUMED):
            group["opened"] = True
            if event_type == AnalyticsEvent.EVENT_STORY_STARTED:
                group["fresh"] = True
        else:  # story_progressed
            milestone = metadata.get("milestone", value)
            if isinstance(milestone, (int, float)):
                group["depth"] = max(group["depth"], float(milestone))
    return {
        "cutoff": cutoff,
        "pairs": pairs,
        "watch": watch,
        "active_days": active_days,
        "session_seconds": session_seconds,
    }


def _opened(group):
    """A session implies the player was opened even if no open event arrived
    (the read-along page emits sessions only)."""
    return group["opened"] or group["seconds"] > 0


def _viewer_pairs(pairs, cutoff):
    """Unique (person, story) pairs who viewed a story page or engaged with it.

    StoryView only knows a user or an IP while events know a user or a browser,
    so an anonymous person can't be matched across the two. Per story the
    larger of the two is taken, so viewers are never fewer than the people who
    started reading it, the same rule the Content tab uses for views."""
    views = {}
    for story_id, user_id, ip in audience_only(filter_since(StoryView.objects.all(), cutoff)).values_list(
        "story_id", "user_id", "ip_address"
    ):
        identity = f"u:{user_id}" if user_id else (f"ip:{ip}" if ip else None)
        if identity:
            views.setdefault(story_id, set()).add(identity)
    engaged = {}
    for (identity, story_id), pair in pairs.items():
        if _opened(pair["read"]) or _opened(pair["listen"]):
            engaged.setdefault(story_id, set()).add(identity)
    return sum(
        max(len(views.get(story_id, ())), len(engaged.get(story_id, ())))
        for story_id in set(views) | set(engaged)
    )


def _funnel_steps(steps):
    """steps: [(key, label, members)] where members maps pair -> signed_in.

    Every step is a subset of the one before it, so counts can only fall. A
    step made only of signed-in people (completion, video progress) is rated
    against the signed-in people of the step before, not everyone."""
    out = []
    previous = None
    for index, (key, label, members, population) in enumerate(steps):
        count = len(members)
        signed_in = sum(1 for flag in members.values() if flag)
        base = None
        rate = None
        if index:
            base = previous["signed_in"] if population == "signed_in" else previous["count"]
            rate = _rate(count, base)
        out.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "population": population,
                "rate": rate,
                "base": base,
                "partial": population == "signed_in",
            }
        )
        previous = {"count": count, "signed_in": signed_in}
    return out


def _build_funnels(scan, viewers):
    pairs = scan["pairs"]
    read_open = {key: p["signed_in"] for key, p in pairs.items() if _opened(p["read"])}
    read_half = {key: flag for key, flag in read_open.items() if _reached(pairs[key]["read"], 0.5)}
    read_done = {key: flag for key, flag in read_half.items() if pairs[key]["read"]["completed"]}
    # Viewers are a count of pairs, not a set of them; carry it as a dict of that size.
    viewer_members = {index: False for index in range(viewers)}
    funnels = {
        "read": _funnel_steps(
            [
                ("viewers", "Viewed the story", viewer_members, "everyone"),
                ("started", "Started reading", read_open, "everyone"),
                ("half", "Reached 50%", read_half, "everyone"),
                ("completed", "Completed", read_done, "signed_in"),
            ]
        )
    }

    listen_open = {key: p["signed_in"] for key, p in pairs.items() if _opened(p["listen"])}
    listen_30 = {key: f for key, f in listen_open.items() if pairs[key]["listen"]["seconds"] >= LISTEN_MIN_SECONDS}
    listen_half = {key: f for key, f in listen_30.items() if _reached(pairs[key]["listen"], 0.5)}
    listen_done = {key: f for key, f in listen_half.items() if pairs[key]["listen"]["completed"]}
    funnels["listen"] = _funnel_steps(
        [
            ("opened", "Opened the audiobook", listen_open, "everyone"),
            ("listened", f"Listened {LISTEN_MIN_SECONDS}s+", listen_30, "everyone"),
            ("half", "Reached 50%", listen_half, "everyone"),
            ("completed", "Completed", listen_done, "signed_in"),
        ]
    )

    # Video pages emit sessions but no start/progress events, so 50% and
    # completion can only come from the signed-in progress table.
    watch_open = {key: key[0].startswith("u:") for key in scan["watch"]}
    watch_30 = {key: f for key, f in watch_open.items() if scan["watch"][key] >= LISTEN_MIN_SECONDS}
    progress = {}
    for user_id, video_id, value in audience_only(
        filter_since(VideoWatchProgress.objects.all(), scan["cutoff"], "updated_at")
    ).values_list("user_id", "video_id", "progress"):
        progress[(f"u:{user_id}", video_id)] = value
    watch_half = {key: True for key in watch_30 if progress.get(key, 0) >= 0.5}
    watch_done = {key: True for key in watch_half if progress[key] >= COMPLETION_THRESHOLD}
    funnels["watch"] = _funnel_steps(
        [
            ("opened", "Started the video", watch_open, "everyone"),
            ("watched", f"Watched {LISTEN_MIN_SECONDS}s+", watch_30, "everyone"),
            ("half", "Reached 50%", watch_half, "signed_in"),
            ("completed", "Completed", watch_done, "signed_in"),
        ]
    )
    return funnels


def _build_depth_curve(scan, lengths):
    """Share of fresh starters still reading at each point, per story length."""
    stats = {bucket: {"starters": 0, "reached": {25: 0, 50: 0, 75: 0}, "signed_in": 0, "completed": 0} for bucket in LENGTH_BUCKETS}
    for (identity, story_id), pair in scan["pairs"].items():
        if not (pair["read"]["fresh"] or pair["listen"]["fresh"]):
            continue
        bucket = lengths.bucket(story_id)
        if bucket is None:
            continue
        entry = stats[bucket]
        entry["starters"] += 1
        depth = max(pair["read"]["depth"], pair["listen"]["depth"])
        for point in (25, 50, 75):
            if depth >= point / 100:
                entry["reached"][point] += 1
        if pair["signed_in"]:
            entry["signed_in"] += 1
            if pair["read"]["completed"] or pair["listen"]["completed"]:
                entry["completed"] += 1
    curve = {}
    for bucket, entry in stats.items():
        starters = entry["starters"]
        points = [{"pct": 0, "rate": 1.0 if starters else None, "reached": starters, "of": starters, "population": "everyone"}]
        for point in (25, 50, 75):
            points.append(
                {
                    "pct": point,
                    "rate": _rate(entry["reached"][point], starters),
                    "reached": entry["reached"][point],
                    "of": starters,
                    "population": "everyone",
                }
            )
        # Finishing is only ever recorded for signed-in people.
        points.append(
            {
                "pct": 100,
                "rate": _rate(entry["completed"], entry["signed_in"]),
                "reached": entry["completed"],
                "of": entry["signed_in"],
                "population": "signed_in",
            }
        )
        curve[bucket] = {"starters": starters, "points": points}
    return curve


def _build_drop_off(scan, cutoff):
    per_story = {}
    for (identity, story_id), pair in scan["pairs"].items():
        if not (_opened(pair["read"]) or _opened(pair["listen"])):
            continue
        depth = max(pair["read"]["depth"], pair["listen"]["depth"])
        done = pair["read"]["completed"] or pair["listen"]["completed"]
        entry = per_story.setdefault(
            story_id,
            {"starters": 0, "finished": 0, "stages": [0, 0, 0, 0], "signed_in": 0, "completed": 0, "completers": set(), "readers": set()},
        )
        entry["starters"] += 1
        if depth >= 0.75 or done:
            entry["finished"] += 1
        stage = 3 if depth >= 0.75 else 2 if depth >= 0.5 else 1 if depth >= 0.25 else 0
        if not done:
            entry["stages"][stage] += 1
        if pair["signed_in"]:
            entry["signed_in"] += 1
            entry["readers"].add(identity)
            if done:
                entry["completed"] += 1
                entry["completers"].add(identity)

    candidates = {sid: e for sid, e in per_story.items() if e["starters"] >= DROP_OFF_MIN_STARTERS}
    if not candidates:
        return []
    titles = {row["id"]: row for row in Story.objects.published().filter(id__in=candidates).values("id", "title", "slug")}

    # Which chapter signed-in readers who didn't finish got to, where known.
    last_chapter = {}
    for user_id, story_id, order, title, progress in audience_only(
        filter_since(ChapterReadingProgress.objects.filter(story_id__in=candidates), cutoff, "updated_at")
    ).values_list("user_id", "story_id", "chapter__order", "chapter__title", "progress"):
        if progress <= 0:
            continue
        key = (story_id, f"u:{user_id}")
        if key not in last_chapter or order > last_chapter[key][0]:
            last_chapter[key] = (order, title)
    chapter_votes = {}
    for (story_id, identity), (order, title) in last_chapter.items():
        if identity in candidates[story_id]["completers"]:
            continue
        votes = chapter_votes.setdefault(story_id, {})
        votes[(order, title)] = votes.get((order, title), 0) + 1

    rows = []
    for story_id, entry in candidates.items():
        story = titles.get(story_id)
        if story is None:
            continue
        stops = entry["stages"]
        stop_index = max(range(4), key=lambda index: (stops[index], -index))
        stopped = sum(stops)
        votes = chapter_votes.get(story_id)
        stop_chapter = None
        if votes:
            (order, title), _count = max(votes.items(), key=lambda item: (item[1], -item[0][0]))
            stop_chapter = {"order": order, "title": title}
        rows.append(
            {
                "id": story_id,
                "title": story["title"],
                "slug": story["slug"],
                "starters": entry["starters"],
                # Reached the last quarter or finished: observable for everyone.
                "finish_rate": _rate(entry["finished"], entry["starters"]),
                # Actually completed: signed-in readers only.
                "completion_rate": _rate(entry["completed"], entry["signed_in"]),
                "signed_in_starters": entry["signed_in"],
                "stop_at": STOP_STAGES[stop_index] if stopped else None,
                "stop_share": _rate(stops[stop_index], entry["starters"]) if stopped else None,
                "stop_chapter": stop_chapter,
            }
        )
    rows.sort(key=lambda row: (row["finish_rate"], -row["starters"], row["title"].lower()))
    return rows[:DROP_OFF_LIMIT]


def _build_feedback(days, cutoff):
    def series(queryset):
        rows = (
            filter_since(audience_only(queryset), cutoff)
            .annotate(day=time_trunc("created_at", days))
            .values("day")
            .annotate(count=Count("id"))
        )
        filled = fill_time_buckets(
            [{"day": row["day"], "count": row["count"]} for row in rows], days, defaults={"count": 0}
        )
        return [row["count"] for row in filled]

    reviews = filter_since(audience_only(Review.objects.all()), cutoff)
    distribution = {row["rating"]: row["n"] for row in reviews.values("rating").annotate(n=Count("id"))}
    favorites = series(Favorite.objects.all())
    reviews_series = series(Review.objects.all())
    reactions = series(StoryReaction.objects.all())
    return {
        "favorites": {"total": sum(favorites), "sparkline": favorites},
        "reviews": {
            "total": sum(reviews_series),
            "sparkline": reviews_series,
            "avg_rating": round(reviews.aggregate(avg=Avg("rating"))["avg"], 2) if reviews.exists() else None,
            "distribution": [{"rating": rating, "count": distribution.get(rating, 0)} for rating in range(1, 6)],
        },
        "reactions": {"total": sum(reactions), "sparkline": reactions},
    }


def _build_other_signals(cutoff):
    counts = {
        row["event_type"]: row["n"]
        for row in audience_events(filter_since(AnalyticsEvent.objects.all(), cutoff))
        .values("event_type")
        .annotate(n=Count("id"))
    }

    def count(event_type):
        return counts.get(event_type, 0)

    started, resumed = count(AnalyticsEvent.EVENT_STORY_STARTED), count(AnalyticsEvent.EVENT_STORY_RESUMED)
    signals = [
        ("resume_rate", "Continue Reading resume rate", "Openings that picked up an unfinished story, over all openings.", resumed, started + resumed),
        (
            "quick_read_conversion",
            "Quick Read conversion",
            "Full-story opens after a summary, over summaries finished.",
            count(AnalyticsEvent.EVENT_QUICK_READ_FULL_STORY_CLICKED),
            count(AnalyticsEvent.EVENT_QUICK_READ_COMPLETED),
        ),
        (
            "daily_story_participation",
            "Daily Story participation",
            "Daily Story opens, over Daily Story views.",
            count(AnalyticsEvent.EVENT_DAILY_STORY_STARTED),
            count(AnalyticsEvent.EVENT_DAILY_STORY_VIEWED),
        ),
        (
            "journey_completion_rate",
            "Journey completion rate",
            "Journeys finished, over journeys begun.",
            count(AnalyticsEvent.EVENT_JOURNEY_COMPLETED),
            count(AnalyticsEvent.EVENT_JOURNEY_STARTED),
        ),
    ]
    return {
        "rates": [
            {"key": key, "label": label, "help": help_text, "numerator": num, "denominator": den, "rate": _rate(num, den)}
            for key, label, help_text, num, den in signals
        ],
        "discovery": {
            "surprise_me_clicked": count(AnalyticsEvent.EVENT_SURPRISE_ME_CLICKED),
            "mood_selected": count(AnalyticsEvent.EVENT_MOOD_SELECTED),
            "next_story_clicked": count(AnalyticsEvent.EVENT_NEXT_STORY_CLICKED),
            "reactions_added": count(AnalyticsEvent.EVENT_REACTION_ADDED),
            "achievements_unlocked": count(AnalyticsEvent.EVENT_ACHIEVEMENT_UNLOCKED),
            "countries_unlocked": count(AnalyticsEvent.EVENT_COUNTRY_UNLOCKED),
        },
    }


def _returning_share(scan, days):
    """Readers who came back: active on another day in this window, or in the
    equal-length window before it."""
    active = scan["active_days"]
    if not active:
        return 0, 0
    prior = set()
    cutoff = scan["cutoff"]
    if cutoff is not None:
        earlier = audience_events(
            filter_since(
                AnalyticsEvent.objects.filter(event_type__in=LIFECYCLE_TYPES[:2] + SESSION_TYPES),
                cutoff - timedelta(days=days),
                until=cutoff,
            )
        ).values_list("user_id", "visitor_id").distinct()
        prior = {_content_identity(user_id, visitor_id) for user_id, visitor_id in earlier}
    returned = sum(1 for identity, dates in active.items() if len(dates) >= 2 or identity in prior)
    return returned, len(active)


def build_engagement_data(days):
    """One window of everything the Engagement tab shows. Wrapped by
    build_engagement_response(), which adds the previous period."""
    cutoff = get_cutoff(days)
    caps = SessionCaps()
    scan = _scan_events(days, caps)
    pairs = scan["pairs"]
    viewers = _viewer_pairs(pairs, cutoff)

    starters = sum(1 for p in pairs.values() if _opened(p["read"]) or _opened(p["listen"]))
    signed_starters = [p for p in pairs.values() if p["signed_in"] and (_opened(p["read"]) or _opened(p["listen"]))]
    completed = sum(1 for p in signed_starters if p["read"]["completed"] or p["listen"]["completed"])
    listeners = [p["listen"] for p in pairs.values() if _opened(p["listen"])]
    people = len(scan["session_seconds"])
    returned, active = _returning_share(scan, days)
    watch_rows = audience_only(filter_since(VideoWatchProgress.objects.all(), cutoff, "updated_at")).aggregate(
        avg=Avg("progress"), n=Count("id")
    )

    all_events = audience_events(filter_since(AnalyticsEvent.objects.all(), cutoff))
    return {
        "range_days": days,
        "time_interval": get_time_interval(days),
        # raw value + the number of people it is a rate over
        "kpis": {
            "start_rate": {"value": _rate(starters, viewers), "sample_size": viewers},
            "completion_rate": {"value": _rate(completed, len(signed_starters)), "sample_size": len(signed_starters)},
            "minutes_per_reader": {
                "value": round(sum(scan["session_seconds"].values()) / 60 / people, 1) if people else None,
                "sample_size": people,
            },
            "returning_rate": {"value": _rate(returned, active), "sample_size": active},
            "audio_listen_through": {
                "value": round(sum(1.0 if g["completed"] else g["depth"] for g in listeners) / len(listeners), 3)
                if listeners
                else None,
                "sample_size": len(listeners),
            },
            "video_watch_through": {
                "value": round(watch_rows["avg"], 3) if watch_rows["avg"] is not None else None,
                "sample_size": watch_rows["n"] or 0,
            },
        },
        "funnels": _build_funnels(scan, viewers),
        "depth_curve": _build_depth_curve(scan, StoryLengths()),
        "drop_off_stories": _build_drop_off(scan, cutoff),
        "retention": {key: _retention(all_events, cutoff, offset) for key, offset in (("d1", 1), ("d7", 7), ("d30", 30))},
        "feedback": _build_feedback(days, cutoff),
        "other_signals": _build_other_signals(cutoff),
    }


KPI_SAMPLE_LABELS = {
    "start_rate": "viewers",
    "completion_rate": "signed-in starters",
    "minutes_per_reader": "readers",
    "returning_rate": "readers",
    "audio_listen_through": "listeners",
    "video_watch_through": "signed-in watchers",
}


def _with_minimum(entry):
    """Hide a value backed by fewer than MIN_SAMPLE people."""
    if entry is None:
        return None, 0
    return (entry["value"] if entry["sample_size"] >= MIN_SAMPLE else None), entry["sample_size"]


def build_engagement_response(days):
    """The Engagement tab payload: the window and how it compares with the one before."""
    data = build_with_previous_period(build_engagement_data, days)
    previous = data.pop("comparison")

    kpis = {}
    for key, entry in data["kpis"].items():
        value, sample = _with_minimum(entry)
        previous_value, previous_sample = _with_minimum(previous["kpis"][key] if previous else None)
        kpis[key] = {
            "value": value,
            "previous_value": previous_value if previous else None,
            "sample_size": sample,
            "previous_sample_size": previous_sample if previous else None,
            "sample_label": KPI_SAMPLE_LABELS[key],
            "min_sample": MIN_SAMPLE,
        }

    funnels = {}
    for fmt, steps in data["funnels"].items():
        prior_steps = {step["key"]: step for step in previous["funnels"][fmt]} if previous else {}
        funnels[fmt] = [
            {
                **step,
                "previous_count": prior_steps[step["key"]]["count"] if step["key"] in prior_steps else None,
                "previous_rate": prior_steps[step["key"]]["rate"] if step["key"] in prior_steps else None,
            }
            for step in steps
        ]

    depth_curve = {}
    for bucket, entry in data["depth_curve"].items():
        prior = previous["depth_curve"][bucket]["points"] if previous else None
        depth_curve[bucket] = {
            "starters": entry["starters"],
            "points": [
                {**point, "previous_rate": prior[index]["rate"] if prior else None}
                for index, point in enumerate(entry["points"])
            ],
        }

    retention = {}
    for key, bucket in data["retention"].items():
        before = previous["retention"][key] if previous else None
        retention[key] = {**bucket, "previous_rate": before["rate"] if before else None}

    feedback = {}
    for key, block in data["feedback"].items():
        before = previous["feedback"][key] if previous else None
        entry = {**block, "previous_total": before["total"] if before else None}
        if key == "reviews":
            entry["previous_avg_rating"] = before["avg_rating"] if before else None
        feedback[key] = entry

    return {
        "range_days": days,
        "time_interval": data["time_interval"],
        "min_sample": MIN_SAMPLE,
        "kpis": kpis,
        "funnels": funnels,
        "depth_curve": depth_curve,
        "drop_off_stories": data["drop_off_stories"],
        "retention": retention,
        "feedback": feedback,
        "other_signals": data["other_signals"],
    }


def build_engagement_export_data(days):
    """build_engagement_response() as the flat tables the CSV/XLSX export is
    made of (see SECTION_TABLE_KEYS["engagement"])."""
    data = build_engagement_response(days)
    return {
        "funnel_steps": [
            {"format": fmt, **{k: v for k, v in step.items()}} for fmt, steps in data["funnels"].items() for step in steps
        ],
        "depth_curve": [
            {"length": bucket, "starters": entry["starters"], **point}
            for bucket, entry in data["depth_curve"].items()
            for point in entry["points"]
        ],
        "drop_off_stories": [
            {**row, "stop_chapter": (row["stop_chapter"] or {}).get("title")} for row in data["drop_off_stories"]
        ],
        "retention": [{"window": key, **bucket} for key, bucket in data["retention"].items()],
        **{f"kpi_{key}": {k: v for k, v in kpi.items() if k != "sample_label"} for key, kpi in data["kpis"].items()},
    }


class AdminAnalyticsEngagementAPIView(APIView):
    """Engagement and the old Metrics tab, merged. Reads AnalyticsEvent so
    anonymous readers count; see the module docstring for what is partial."""

    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_engagement_response(get_range_days(request)))

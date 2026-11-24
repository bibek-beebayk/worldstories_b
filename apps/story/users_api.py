"""Analytics -> Users tab: who signed up, where from, whether they stuck, and a
row per registered reader.

Staff and superusers are excluded everywhere (the same rule as the rest of the
admin analytics), and so are events flagged as bots. Minutes use SessionCaps, so
an idle tab can't inflate a reader's time.

Linking a user to their pre-signup visits: events carry the browser's visitor_id,
and once someone is logged in they also carry the user. So a user's visitor_ids
are the ones on their own events (minus the server's placeholder), and their
*anonymous* events under those same visitor_ids are what they did before signing
up. A person who signs up on a device they never used logged out has no such
trail; their source falls back to their first visit after signing up, and failing
that is "unknown".

What is not recorded, and so can't appear here: there is no event for opening the
signup form, so the funnel has no "signup started" step; OTP verification has a
flag but no timestamp, so it is a current state, not a step in time.
"""
from collections import Counter
from datetime import timedelta

from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.stats.models import (
    AnalyticsEvent,
    AudioReadingProgress,
    BlogReadingProgress,
    ChapterReadingProgress,
    FileReadingProgress,
    QuickReadProgress,
    ReadingProgress,
    StoryCompletion,
    UserAchievement,
    VideoWatchProgress,
)
from apps.stats.streaks import compute_streak
from apps.users.models import User, UserLoginLocation

from .analytics_api import (
    _PERIOD_END,
    CACHE_SECONDS,
    audience_events,
    audience_only,
    build_with_previous_period,
    get_cutoff,
    get_range_days,
    get_time_interval,
    normalize_referral_source,
    filter_since,
)
from .api import IsSuperUser
from .audience_api import resolve_paths
from .content_scoring import SessionCaps
from .models import Blog, Favorite, Review, Story, StoryJourney, StoryReaction, Video

MIN_SAMPLE = 5
ACTIVATION_WINDOW_DAYS = 7
NEW_USER_DAYS = 14
ACTIVE_DAYS = 14
AT_RISK_DAYS = 30
COHORT_MIN_USERS = 30
COHORT_WEEKS = 4
COHORT_LIMIT = 12
USERS_PAGE_SIZE = 500
TIMELINE_LIMIT = 100
SPARKLINE_WEEKS = 12
UNKNOWN_SOURCE = "unknown"
SERVER_VISITOR_ID = AnalyticsEvent.SERVER_VISITOR_ID

STORY_ACTIVITY_TYPES = (
    AnalyticsEvent.EVENT_STORY_STARTED,
    AnalyticsEvent.EVENT_STORY_RESUMED,
    AnalyticsEvent.EVENT_STORY_COMPLETED,
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
)
SESSION_TYPES = (
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
)
PROGRESS_MODELS = (
    ReadingProgress,
    ChapterReadingProgress,
    AudioReadingProgress,
    VideoWatchProgress,
    FileReadingProgress,
    BlogReadingProgress,
    QuickReadProgress,
)
STATUSES = ("new", "active", "at_risk", "dormant", "unverified")


def audience_users():
    """Registered readers: staff and superusers are operators, not audience."""
    return User.objects.filter(is_superuser=False, is_staff=False)


def classify_status(date_joined, last_active, otp_verified, now):
    """unverified > new > active > at_risk > dormant.

    `new` is joined within 14 days and `active` is active within 14 days (both
    inclusive); `at_risk` is last active 14-30 days ago; anything longer, or
    never active, is `dormant`. Unverified accounts are shown as such however
    recently they were active, because they can't use the app properly."""
    if not otp_verified:
        return "unverified"
    if date_joined >= now - timedelta(days=NEW_USER_DAYS):
        return "new"
    if last_active is None:
        return "dormant"
    age = now - last_active
    if age <= timedelta(days=ACTIVE_DAYS):
        return "active"
    if age <= timedelta(days=AT_RISK_DAYS):
        return "at_risk"
    return "dormant"


def _chunked(values, size=500):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _local_date(moment):
    return timezone.localtime(moment).date()


def _week_start(day):
    return day - timedelta(days=day.weekday())


# --- one pass over what each user did ------------------------------------------------


def _scan_activity(days, caps):
    """Every registered reader's activity: dates, last activity, first story
    activity, and (for the window) stories started / completed and capped minutes."""
    start = get_cutoff(days)
    end = _PERIOD_END.get()

    def in_window(moment):
        return (start is None or moment >= start) and (end is None or moment < end)

    out = {}

    def entry(user_id):
        return out.setdefault(
            user_id,
            {
                "dates": set(),
                "window_dates": set(),
                "last": None,
                "first_story": None,
                "started": set(),
                "completed": set(),
                "minutes": {"reading": 0.0, "listening": 0.0, "watching": 0.0},
            },
        )

    def touch(item, moment):
        item["dates"].add(_local_date(moment))
        if item["last"] is None or moment > item["last"]:
            item["last"] = moment
        if in_window(moment):
            item["window_dates"].add(_local_date(moment))

    events = audience_events(AnalyticsEvent.objects.filter(user__isnull=False)).values_list(
        "user_id", "event_type", "created_at", "story_id", "video_id", "blog_id", "duration_seconds", "metadata"
    )
    for user_id, event_type, created_at, story_id, video_id, blog_id, duration, metadata in events:
        item = entry(user_id)
        touch(item, created_at)
        if event_type in STORY_ACTIVITY_TYPES and (story_id or video_id) and not blog_id:
            if item["first_story"] is None or created_at < item["first_story"]:
                item["first_story"] = created_at
        if not in_window(created_at):
            continue
        if event_type == AnalyticsEvent.EVENT_STORY_STARTED and story_id:
            item["started"].add(story_id)
        elif event_type == AnalyticsEvent.EVENT_STORY_COMPLETED and story_id:
            item["completed"].add(story_id)
        elif event_type in SESSION_TYPES:
            quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and (metadata or {}).get("format") == "quick_read"
            seconds = caps.capped(
                duration, event_type, story_id=story_id, blog_id=blog_id, video_id=video_id, is_quick_read=quick
            )
            key = {
                AnalyticsEvent.EVENT_READING_SESSION: "reading",
                AnalyticsEvent.EVENT_LISTENING_SESSION: "listening",
                AnalyticsEvent.EVENT_WATCHING_SESSION: "watching",
            }[event_type]
            item["minutes"][key] += seconds / 60

    for model in PROGRESS_MODELS:
        for user_id, updated_at in audience_only(model.objects.all()).values_list("user_id", "updated_at"):
            touch(entry(user_id), updated_at)
    return out


# --- signup attribution ----------------------------------------------------------------


def _signup_attribution(users):
    """{user_id: {source, landing_path, story_id, method}} — see the module docstring."""
    joined = {user.pk: user.date_joined for user in users}
    visitor_ids = {}
    for user_id, visitor_id in (
        audience_events(AnalyticsEvent.objects.filter(user__isnull=False))
        .exclude(visitor_id=SERVER_VISITOR_ID)
        .values_list("user_id", "visitor_id")
        .distinct()
    ):
        visitor_ids.setdefault(user_id, set()).add(visitor_id)

    owners = {}  # visitor_id -> user ids (a shared browser links to several)
    for user_id, ids in visitor_ids.items():
        for visitor_id in ids:
            owners.setdefault(visitor_id, set()).add(user_id)

    trail = {user_id: [] for user_id in joined}  # (created_at, type, story_id, source, path)
    for chunk in _chunked(owners):
        rows = audience_events(AnalyticsEvent.objects.filter(visitor_id__in=chunk)).values_list(
            "visitor_id", "user_id", "event_type", "created_at", "story_id", "metadata"
        )
        for visitor_id, event_user_id, event_type, created_at, story_id, metadata in rows:
            for user_id in owners[visitor_id]:
                # Another user's own events on a shared browser aren't this user's trail.
                if event_user_id not in (None, user_id):
                    continue
                metadata = metadata or {}
                trail[user_id].append((created_at, event_type, story_id, metadata.get("referral_source"), metadata.get("path")))

    result = {}
    for user_id, when in joined.items():
        events = sorted(trail.get(user_id, []), key=lambda row: row[0])
        visits_before = [(created_at, source, path) for created_at, kind, _story, source, path in events if kind == AnalyticsEvent.EVENT_VISIT and created_at <= when]
        visits_after = [(created_at, source, path) for created_at, kind, _story, source, path in events if kind == AnalyticsEvent.EVENT_VISIT and created_at > when]
        source = path = None
        method = "none"
        for method_name, visits in (("before_signup", visits_before), ("after_signup", visits_after)):
            for created_at, raw, visit_path in visits:  # earliest first
                if normalize_referral_source(raw) != "internal":
                    source, path, method = normalize_referral_source(raw), visit_path, method_name
                    break
            if source:
                break
        story_id = None
        for created_at, _kind, event_story, _source, _path in reversed(events):
            if event_story and created_at <= when:
                story_id = event_story
                break
        result[user_id] = {"source": source, "landing_path": path, "story_id": story_id, "method": method}
    return result


# --- the period builder (KPIs and funnel) ------------------------------------------------


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def build_users_period(days):
    """One window of the KPIs and the signup funnel. Wrapped by
    build_users_response(), which adds the previous period."""
    now = timezone.now()
    cutoff = get_cutoff(days)
    caps = SessionCaps()
    people = list(audience_users().values("id", "date_joined", "otp_verified"))
    activity = _scan_activity(days, caps)

    registered = [row for row in people if row["date_joined"] <= now]
    end = _PERIOD_END.get()
    new_users = [
        row
        for row in registered
        if (cutoff is None or row["date_joined"] >= cutoff) and (end is None or row["date_joined"] < end)
    ]
    audience_ids = {row["id"] for row in registered}
    active = {uid for uid, item in activity.items() if uid in audience_ids and item["window_dates"]}

    # Activation: started a story within a week of joining, among new users
    # whose first week is over (so a user who joined yesterday isn't a failure).
    window = timedelta(days=ACTIVATION_WINDOW_DAYS)
    eligible = [row for row in new_users if row["date_joined"] <= now - window]
    activated = [
        row
        for row in eligible
        if activity.get(row["id"], {}).get("first_story") and activity[row["id"]]["first_story"] <= row["date_joined"] + window
    ]

    anonymous_visitors = (
        audience_events(
            filter_since(
                AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_VISIT, user__isnull=True), cutoff
            )
        )
        .values("visitor_id")
        .distinct()
        .count()
    )
    visitor_identities = {
        ("u", user_id) if user_id else ("v", visitor_id)
        for user_id, visitor_id in audience_events(
            filter_since(AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_VISIT), cutoff)
        )
        .values_list("user_id", "visitor_id")
        .distinct()
    }

    # Minutes, signed-in vs everyone.
    signed_in_minutes = sum(sum(item["minutes"].values()) for item in activity.values())
    total_seconds = 0.0
    people_with_time = set()
    for user_id, visitor_id, event_type, duration, story_id, video_id, blog_id, metadata in audience_events(
        filter_since(AnalyticsEvent.objects.filter(event_type__in=SESSION_TYPES), cutoff)
    ).values_list("user_id", "visitor_id", "event_type", "duration_seconds", "story_id", "video_id", "blog_id", "metadata"):
        quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and (metadata or {}).get("format") == "quick_read"
        total_seconds += caps.capped(
            duration, event_type, story_id=story_id, blog_id=blog_id, video_id=video_id, is_quick_read=quick
        )
        people_with_time.add(("u", user_id) if user_id else ("v", visitor_id))

    # The signup funnel is nested: each step is a subset of the one before.
    created = {row["id"] for row in new_users}
    verified = {row["id"] for row in new_users if row["otp_verified"]} & created
    completions = set(
        audience_only(StoryCompletion.objects.all()).values_list("user_id", flat=True).distinct()
    )
    started_ids = {uid for uid in verified if activity.get(uid, {}).get("first_story") or uid in completions}
    completed_ids = started_ids & completions
    # Nested like every other step, so the funnel can only narrow. (It does mean
    # someone who started, came back and never finished isn't counted here; the
    # activation KPI and the cohort grid are where that retention shows up.)
    second_day = {uid for uid in completed_ids if len(activity.get(uid, {}).get("dates", ())) >= 2}
    funnel = [
        {"key": "visitors", "label": "Visitors", "count": len(visitor_identities)},
        {"key": "account_created", "label": "Account created", "count": len(created)},
        {"key": "otp_verified", "label": "OTP verified", "count": len(verified)},
        {"key": "first_story_started", "label": "Started a story", "count": len(started_ids)},
        {"key": "first_story_completed", "label": "Completed a story", "count": len(completed_ids)},
        {"key": "second_day", "label": "Active on a 2nd day", "count": len(second_day)},
    ]

    total_minutes_all = total_seconds / 60
    return {
        "range_days": days,
        "kpi_values": {
            "registered_users": {"value": len(registered), "sample_size": len(registered)},
            "new_users": {"value": len(new_users), "sample_size": len(new_users)},
            "active_users": {"value": len(active), "sample_size": len(registered)},
            "activation_rate": {"value": _rate(len(activated), len(eligible)), "sample_size": len(eligible)},
            "visitor_to_signup_rate": {"value": _rate(len(new_users), anonymous_visitors), "sample_size": anonymous_visitors},
            "signed_in_minutes_share": {
                "value": _rate(min(signed_in_minutes, total_minutes_all), total_minutes_all),
                "sample_size": len(people_with_time),
            },
        },
        "funnel": funnel,
        "join_dates": [row["date_joined"] for row in registered],
    }


# --- the table, segments, signups and cohorts (current period only) -----------------------


def _all_time_counts(model, key="user_id"):
    return dict(audience_only(model.objects.all()).values_list(key).annotate(n=Count("id")))


def _journeys_completed(completed_by_user):
    journeys = []
    for journey in StoryJourney.objects.filter(active=True).prefetch_related("items"):
        required = {item.story_id for item in journey.items.all() if item.required}
        if required:
            journeys.append(required)
    return {user_id: sum(1 for required in journeys if required <= stories) for user_id, stories in completed_by_user.items()}


def _bucket_signups(days, today):
    """Weekly buckets (Monday) for 30+ day ranges, daily for 7 days or less."""
    return "day" if days != "all" and days <= 7 else "week"


def build_users_table(days, users_page=1):
    now = timezone.now()
    today = timezone.localdate()
    caps = SessionCaps()
    users = list(audience_users().order_by("-date_joined"))
    activity = _scan_activity(days, caps)
    attribution = _signup_attribution(users)

    completions = {}
    countries = {}
    for user_id, story_id, country in audience_only(StoryCompletion.objects.all()).values_list(
        "user_id", "story_id", "story__country"
    ):
        completions.setdefault(user_id, set()).add(story_id)
        if country:
            countries.setdefault(user_id, set()).add(country)
    journeys = _journeys_completed(completions)
    achievements = dict(
        audience_only(UserAchievement.objects.filter(completed=True)).values_list("user_id").annotate(n=Count("id"))
    )
    favorites, reviews, reactions = (
        _all_time_counts(Favorite),
        _all_time_counts(Review),
        _all_time_counts(StoryReaction),
    )
    login_country = {}
    for user_id, country, code in UserLoginLocation.objects.order_by("user_id", "-created_at").values_list(
        "user_id", "country", "country_code"
    ):
        if user_id not in login_country and (country or code):
            login_country[user_id] = country or code

    titles = {pk: (title, slug) for pk, title, slug in Story.objects.filter(
        pk__in={a["story_id"] for a in attribution.values() if a["story_id"]}
    ).values_list("id", "title", "slug")}
    resolved = resolve_paths(a["landing_path"] for a in attribution.values())

    rows = []
    for user in users:
        item = activity.get(user.pk)
        last_active = item["last"] if item else None
        attr = attribution[user.pk]
        landing = resolved.get(attr["landing_path"]) if attr["landing_path"] else None
        story = titles.get(attr["story_id"])
        current, longest = compute_streak(item["dates"] if item else set(), today)
        minutes = item["minutes"] if item else {"reading": 0.0, "listening": 0.0, "watching": 0.0}
        rows.append(
            {
                "id": str(user.pk),
                "username": user.username,
                "display_name": user.display_name,
                "email": user.email,
                "avatar_url": user.avatar_url,
                "date_joined": user.date_joined,
                "last_login": user.last_login,
                "login_count": user.login_count,
                "last_active": last_active,
                "otp_verified": user.otp_verified,
                "status": classify_status(user.date_joined, last_active, user.otp_verified, now),
                "signup_source": attr["source"],
                "signup_method": attr["method"],
                "signup_landing": (
                    {"path": landing["path"], "title": landing["title"], "type": landing["type"], "resolved": landing["resolved"]}
                    if landing
                    else None
                ),
                "signup_story": {"title": story[0], "slug": story[1]} if story else None,
                "active_days": len(item["window_dates"]) if item else 0,
                "stories_started": len(item["started"]) if item else 0,
                "stories_completed": len(item["completed"]) if item else 0,
                "reading_minutes": round(minutes["reading"], 1),
                "listening_minutes": round(minutes["listening"], 1),
                "watching_minutes": round(minutes["watching"], 1),
                "favorites": favorites.get(user.pk, 0),
                "reviews": reviews.get(user.pk, 0),
                "reactions": reactions.get(user.pk, 0),
                "current_streak": current,
                "longest_streak": longest,
                "achievements_count": achievements.get(user.pk, 0),
                "passport_countries": len(countries.get(user.pk, ())),
                "journeys_completed": journeys.get(user.pk, 0),
                "country": login_country.get(user.pk),
            }
        )

    # Newest activity first; people who have never been active last.
    rows.sort(key=lambda row: (row["last_active"] is None, -(row["last_active"].timestamp() if row["last_active"] else 0)))
    segments = Counter(row["status"] for row in rows)

    attributed = sum(1 for row in rows if row["signup_source"])
    methods = Counter(row["signup_method"] for row in rows)

    # Signups over time, by source.
    cutoff = get_cutoff(days)
    interval = _bucket_signups(days, today)
    if cutoff is not None:
        start_day = _local_date(cutoff)
    elif rows:
        start_day = _local_date(min(row["date_joined"] for row in rows))
    else:
        start_day = today
    bucket_of = (lambda d: d) if interval == "day" else _week_start
    keys, cursor = [], bucket_of(start_day)
    step = timedelta(days=1 if interval == "day" else 7)
    while cursor <= today:
        keys.append(cursor)
        cursor += step
    keys = keys[-104:]
    by_bucket = {key: Counter() for key in keys}
    for row in rows:
        joined = _local_date(row["date_joined"])
        if cutoff is not None and row["date_joined"] < cutoff:
            continue
        key = bucket_of(joined)
        if key in by_bucket:
            by_bucket[key][row["signup_source"] or UNKNOWN_SOURCE] += 1
    source_totals = Counter()
    for counter in by_bucket.values():
        source_totals.update(counter)
    signups = {
        "interval": interval,
        "sources": [source for source, _ in sorted(source_totals.items(), key=lambda item: (-item[1], item[0]))],
        "series": [{"day": key, "total": sum(by_bucket[key].values()), "by_source": dict(by_bucket[key])} for key in keys],
    }

    total_pages = max(1, -(-len(rows) // USERS_PAGE_SIZE))
    page = min(max(1, users_page), total_pages)
    return {
        "users": rows[(page - 1) * USERS_PAGE_SIZE : page * USERS_PAGE_SIZE],
        "users_total": len(rows),
        "users_page": page,
        "users_pages": total_pages,
        "segments": {status: segments.get(status, 0) for status in STATUSES},
        "signups_over_time": signups,
        "cohorts": _build_cohorts(users, activity, today),
        "attribution": {
            "attributed": attributed,
            "total": len(rows),
            "before_signup": methods.get("before_signup", 0),
            "after_signup": methods.get("after_signup", 0),
            "none": methods.get("none", 0),
        },
    }


def _build_cohorts(users, activity, today):
    if len(users) < COHORT_MIN_USERS:
        return {"available": False, "min_users": COHORT_MIN_USERS, "total_users": len(users), "weeks": COHORT_WEEKS, "rows": []}
    cohorts = {}
    for user in users:
        joined = _local_date(user.date_joined)
        dates = activity.get(user.pk, {}).get("dates", set())
        cohort = cohorts.setdefault(_week_start(joined), {"size": 0, "eligible": [0] * COHORT_WEEKS, "active": [0] * COHORT_WEEKS})
        cohort["size"] += 1
        for week in range(1, COHORT_WEEKS + 1):
            # Week k is days 7k..7k+6 after joining, judged only once it is over.
            if joined + timedelta(days=7 * (week + 1)) > today:
                continue
            cohort["eligible"][week - 1] += 1
            if any(7 * week <= (d - joined).days < 7 * week + 7 for d in dates):
                cohort["active"][week - 1] += 1
    rows = [
        {
            "cohort": start,
            "size": data["size"],
            "weeks": [
                {"week": week + 1, "eligible": data["eligible"][week], "active": data["active"][week], "rate": _rate(data["active"][week], data["eligible"][week])}
                for week in range(COHORT_WEEKS)
            ],
        }
        for start, data in sorted(cohorts.items(), reverse=True)[:COHORT_LIMIT]
    ]
    return {"available": True, "min_users": COHORT_MIN_USERS, "total_users": len(users), "weeks": COHORT_WEEKS, "rows": rows}


# --- the response -----------------------------------------------------------------------------


def _with_minimum(entry, count_only):
    if entry is None:
        return None, 0
    return (entry["value"] if (count_only or entry["sample_size"] >= MIN_SAMPLE) else None), entry["sample_size"]


def build_users_response(days, users_page=1):
    data = build_with_previous_period(build_users_period, days)
    previous = data.pop("comparison")
    kpis = {}
    for key, entry in data["kpi_values"].items():
        count_only = key in ("registered_users", "new_users", "active_users")
        value, sample = _with_minimum(entry, count_only)
        before, before_sample = _with_minimum(previous["kpi_values"][key] if previous else None, count_only)
        kpis[key] = {
            "value": value,
            "previous_value": before if previous else None,
            "sample_size": sample,
            "previous_sample_size": before_sample if previous else None,
            "min_sample": MIN_SAMPLE,
        }

    # Weekly total-users sparkline, ending now.
    now = timezone.now()
    joins = data["join_dates"]
    kpis["registered_users"]["sparkline"] = [
        sum(1 for joined in joins if joined <= now - timedelta(days=7 * offset)) for offset in range(SPARKLINE_WEEKS - 1, -1, -1)
    ]

    prior_steps = {step["key"]: step for step in previous["funnel"]} if previous else {}
    funnel = []
    for index, step in enumerate(data["funnel"]):
        before = data["funnel"][index - 1]["count"] if index else None
        funnel.append(
            {
                **step,
                "rate": _rate(step["count"], before) if before is not None else None,
                "previous_count": prior_steps[step["key"]]["count"] if step["key"] in prior_steps else None,
            }
        )

    table = build_users_table(days, users_page)
    return {
        "range_days": days,
        "time_interval": get_time_interval(days),
        "min_sample": MIN_SAMPLE,
        "kpis": kpis,
        # "Signup started" is deliberately absent: nothing records opening the signup form.
        "signup_funnel": funnel,
        **table,
    }


class AdminAnalyticsUsersAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        try:
            page = int(request.query_params.get("users_page", 1))
        except (TypeError, ValueError):
            page = 1
        return Response(build_users_response(get_range_days(request), page))


# --- one user's timeline ------------------------------------------------------------------------


def build_user_timeline(user):
    caps = SessionCaps()
    sessions = {}
    for event_type, created_at, story_id, blog_id, video_id, duration, metadata in audience_events(
        AnalyticsEvent.objects.filter(user=user, event_type__in=SESSION_TYPES)
    ).values_list("event_type", "created_at", "story_id", "blog_id", "video_id", "duration_seconds", "metadata"):
        quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and (metadata or {}).get("format") == "quick_read"
        if event_type == AnalyticsEvent.EVENT_WATCHING_SESSION:
            kind, target = "video_session", video_id
        elif event_type == AnalyticsEvent.EVENT_LISTENING_SESSION:
            kind, target = "audio_session", story_id
        elif blog_id:
            kind, target = "blog_session", blog_id
        else:
            kind, target = ("quick_read_session" if quick else "story_session"), story_id
        if not target:
            continue
        key = (kind, target, _local_date(created_at))
        item = sessions.setdefault(key, {"at": created_at, "seconds": 0.0, "sessions": 0})
        item["seconds"] += caps.capped(
            duration, event_type, story_id=story_id, blog_id=blog_id, video_id=video_id, is_quick_read=quick
        )
        item["sessions"] += 1
        item["at"] = max(item["at"], created_at)

    story_ids = {t for (k, t, _d) in sessions if k in ("story_session", "audio_session", "quick_read_session")}
    story_titles = {pk: title for pk, title in Story.objects.filter(pk__in=story_ids).values_list("id", "title")}
    blog_titles = {pk: title for pk, title in Blog.objects.filter(pk__in={t for (k, t, _d) in sessions if k == "blog_session"}).values_list("id", "title")}
    video_titles = {pk: title for pk, title in Video.objects.filter(pk__in={t for (k, t, _d) in sessions if k == "video_session"}).values_list("id", "title")}

    chapter_counts = dict(Story.objects.filter(pk__in=story_ids).annotate(n=Count("chapters")).values_list("id", "n"))
    chapter_progress, audio_progress, video_progress, blog_progress = {}, {}, {}, {}
    for story_id, progress in ChapterReadingProgress.objects.filter(user=user).values_list("story_id", "progress"):
        chapter_progress[story_id] = chapter_progress.get(story_id, 0) + progress
    for story_id, progress in ReadingProgress.objects.filter(user=user).values_list("story_id", "progress"):
        chapter_progress.setdefault(story_id, progress * max(chapter_counts.get(story_id, 1), 1))
    audio_counts = dict(
        Story.objects.filter(pk__in=story_ids)
        .annotate(n=Count("audiobooks__tracks", distinct=True))
        .values_list("id", "n")
    )
    for story_id, progress in AudioReadingProgress.objects.filter(
        user=user, audiobook__story_id__isnull=False
    ).values_list("audiobook__story_id", "progress"):
        audio_progress[story_id] = audio_progress.get(story_id, 0) + progress
    for video_id, progress in VideoWatchProgress.objects.filter(user=user).values_list("video_id", "progress"):
        video_progress[video_id] = progress
    for blog_id, progress in BlogReadingProgress.objects.filter(user=user).values_list("blog_id", "progress"):
        blog_progress[blog_id] = progress

    def story_progress(story_id):
        chapters = max(chapter_counts.get(story_id, 0), 1)
        return round(min(1.0, chapter_progress[story_id] / chapters), 3) if story_id in chapter_progress else None

    def listened_progress(story_id):
        tracks = max(audio_counts.get(story_id, 0), 1)
        return round(min(1.0, audio_progress[story_id] / tracks), 3) if story_id in audio_progress else None

    items = []
    for (kind, target, _day), item in sessions.items():
        if kind == "blog_session":
            title, progress = blog_titles.get(target), blog_progress.get(target)
        elif kind == "video_session":
            title, progress = video_titles.get(target), video_progress.get(target)
        elif kind == "audio_session":
            title, progress = story_titles.get(target), listened_progress(target)
        else:
            title, progress = story_titles.get(target), story_progress(target) if kind == "story_session" else None
        items.append(
            {
                "kind": kind,
                "at": item["at"],
                "title": title or "Deleted item",
                "minutes": round(item["seconds"] / 60, 1),
                "sessions": item["sessions"],
                "progress": progress,
            }
        )
    for completed_at, title, source in StoryCompletion.objects.filter(user=user).values_list("completed_at", "story__title", "source"):
        items.append({"kind": "completion", "at": completed_at, "title": title, "source": source})
    for created_at, title in Favorite.objects.filter(user=user).values_list("created_at", "story__title"):
        items.append({"kind": "favorite", "at": created_at, "title": title})
    for created_at, title, rating in Review.objects.filter(user=user).values_list("created_at", "story__title", "rating"):
        items.append({"kind": "review", "at": created_at, "title": title, "rating": rating})
    for completed_at, name in UserAchievement.objects.filter(user=user, completed=True, completed_at__isnull=False).values_list(
        "completed_at", "achievement__name"
    ):
        items.append({"kind": "achievement", "at": completed_at, "title": name})

    items.sort(key=lambda item: item["at"], reverse=True)
    return {"user_id": str(user.pk), "total": len(items), "items": items[:TIMELINE_LIMIT]}


class AdminUserTimelineAPIView(APIView):
    """A reader's recent activity, newest first, capped at 100 items. Operators
    (staff / superusers) have no timeline: they aren't audience."""

    permission_classes = [IsSuperUser]

    def get(self, request, user_id):
        user = get_object_or_404(audience_users(), pk=user_id)
        return Response(build_user_timeline(user))


def build_users_export_data(days):
    """build_users_response() as the flat tables the CSV/XLSX export is made of
    (see SECTION_TABLE_KEYS["users"]). Email is left out of the export."""
    data = build_users_response(days)
    return {
        "users": [
            {
                **{k: v for k, v in row.items() if k not in ("signup_landing", "signup_story", "email", "avatar_url")},
                "signup_landing": (row["signup_landing"] or {}).get("title"),
                "signup_story": (row["signup_story"] or {}).get("title"),
            }
            for row in data["users"]
        ],
        "signup_funnel": data["signup_funnel"],
        "signups_over_time": [
            {"day": point["day"], "total": point["total"], **{source: point["by_source"].get(source, 0) for source in data["signups_over_time"]["sources"]}}
            for point in data["signups_over_time"]["series"]
        ],
        "cohorts": [
            {"cohort": row["cohort"], "size": row["size"], **{f"week_{w['week']}_rate": w["rate"] for w in row["weeks"]}}
            for row in data["cohorts"]["rows"]
        ],
        **{f"kpi_{key}": {k: v for k, v in kpi.items() if k != "sparkline"} for key, kpi in data["kpis"].items()},
        "segments": data["segments"],
    }

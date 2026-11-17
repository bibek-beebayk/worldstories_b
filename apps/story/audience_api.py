"""Analytics -> Audience tab (and the Monetization tab split out of it).

Everything is computed from audience-filtered AnalyticsEvent rows — staff and
suspected bots are dropped — and a "visitor" is the signed-in user, else the
browser's visitor_id (see _content_identity), so a person on two devices counts
once. Minutes use SessionCaps, the same per-session cap the Content tab uses, so
an idle tab can't inflate them.

What a "session" is here: the browser's session_id (a tab's sessionStorage), or
the visitor's day when an event has none. A visit event is a page view; the
first one in a session is its entry (landing) page.
"""
import re
import statistics
from collections import Counter

from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.pages.models import Page
from apps.stats.models import AnalyticsEvent, BotFlagRun

from .analytics_api import (
    CACHE_SECONDS,
    _content_identity,
    audience_events,
    audience_only,
    build_publish_markers,
    build_with_previous_period,
    fill_time_buckets,
    filter_since,
    first_touch_sources,
    get_cutoff,
    get_range_days,
    get_time_interval,
    python_time_bucket,
)
from .api import IsSuperUser
from .content_scoring import SessionCaps
from .models import Blog, Story

# A rate over fewer visitors than this says nothing, so it is reported as null.
MIN_SAMPLE = 5
# "Engaged" = any of: this much active time, this many page views in a session,
# or any reading / listening / watching activity.
ENGAGED_ACTIVE_SECONDS = 30
ENGAGED_PAGE_VIEWS = 2
# Time between two page loads counts as browsing only up to this, so a tab left
# open for an hour isn't an hour of activity.
BROWSING_GAP_CAP_SECONDS = 5 * 60
LANDING_PAGE_LIMIT = 20
SLUG_WARNING_LIMIT = 10
# The default slug an import or a draft gets before anyone names the story.
PLACEHOLDER_SLUG = re.compile(r"^story-\d+$")

SCAN_TYPES = (
    AnalyticsEvent.EVENT_VISIT,
    AnalyticsEvent.EVENT_STORY_STARTED,
    AnalyticsEvent.EVENT_STORY_RESUMED,
    AnalyticsEvent.EVENT_STORY_COMPLETED,
    AnalyticsEvent.EVENT_COMPLETION,
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
)
STARTED_TYPES = (AnalyticsEvent.EVENT_STORY_STARTED, AnalyticsEvent.EVENT_STORY_RESUMED)
SESSION_TYPES = (
    AnalyticsEvent.EVENT_READING_SESSION,
    AnalyticsEvent.EVENT_LISTENING_SESSION,
    AnalyticsEvent.EVENT_WATCHING_SESSION,
)
FORMATS = ("read", "listen", "watch", "quick_read", "blog")


# --- path resolution ------------------------------------------------------------

_STORY = re.compile(r"^/story/(?P<slug>[^/]+)(?:/(?:pdf|epub))?/?$")
_READ = re.compile(r"^/read/(?P<slug>[^/]+)(?:/[^/]+)?/?$")
_LISTEN = re.compile(r"^/(?:listen|read-along)/(?P<slug>[^/]+)(?:/[^/]+)?/?$")
_WATCH = re.compile(r"^/watch/(?P<slug>[^/]+)/?$")
_BLOG = re.compile(r"^/blog/(?P<slug>[^/]+)/?$")
_PAGE = re.compile(r"^/pages/(?P<slug>[^/]+)/?$")
_STORY_KINDS = (("story", _STORY), ("read", _READ), ("listen", _LISTEN), ("watch", _WATCH))


def resolve_paths(paths):
    """Turn raw URL paths into {path: {type, title, admin_link, resolved}}.

    Slugs are matched against Story, Blog and Page. A path that matches no
    known route or no known slug keeps the raw path as its title and is
    `resolved: False`."""
    paths = {path for path in paths if path}
    matches = {}
    for path in paths:
        if path == "/":
            matches[path] = ("home", None, None)
        elif path.rstrip("/") == "/library":
            matches[path] = ("library", None, None)
        else:
            for kind, pattern in _STORY_KINDS:
                found = pattern.match(path)
                if found:
                    matches[path] = (kind, "story", found["slug"])
                    break
            else:
                for kind, pattern, model in (("blog", _BLOG, "blog"), ("page", _PAGE, "page")):
                    found = pattern.match(path)
                    if found:
                        matches[path] = (kind, model, found["slug"])
                        break

    wanted = {model: {slug for _kind, m, slug in matches.values() if m == model and slug} for model in ("story", "blog", "page")}
    titles = {
        "story": {slug: (pk, title) for pk, slug, title in Story.objects.filter(slug__in=wanted["story"]).values_list("id", "slug", "title")},
        "blog": {slug: (pk, title) for pk, slug, title in Blog.objects.filter(slug__in=wanted["blog"]).values_list("id", "slug", "title")},
        "page": {slug: (pk, title) for pk, slug, title in Page.objects.filter(slug__in=wanted["page"]).values_list("id", "slug", "title")},
    }
    admin_links = {
        "story": lambda pk: f"/admin/content?storyId={pk}",
        "blog": lambda pk: "/admin/blog",
        "page": lambda pk: "/admin/pages",
    }
    resolved = {}
    for path in paths:
        match = matches.get(path)
        if match is None:
            resolved[path] = {"path": path, "type": "other", "title": path, "admin_link": None, "resolved": False}
            continue
        kind, model, slug = match
        if model is None:
            resolved[path] = {
                "path": path, "type": kind, "title": "Home" if kind == "home" else "Library", "admin_link": None, "resolved": True,
            }
        elif slug in titles[model]:
            pk, title = titles[model][slug]
            resolved[path] = {"path": path, "type": kind, "title": title, "admin_link": admin_links[model](pk), "resolved": True}
        else:
            resolved[path] = {"path": path, "type": "other", "title": path, "admin_link": None, "resolved": False}
    return resolved


def build_slug_warnings():
    """Published stories still carrying a placeholder slug (story-12): their
    public URL says nothing and is hard to find in analytics."""
    stories = Story.objects.published().filter(slug__regex=PLACEHOLDER_SLUG.pattern)
    return {
        "total": stories.count(),
        "stories": [
            {"id": pk, "title": title, "slug": slug, "admin_link": f"/admin/content?storyId={pk}"}
            for pk, title, slug in stories.order_by("id").values_list("id", "title", "slug")[:SLUG_WARNING_LIMIT]
        ],
    }


# --- the scan ----------------------------------------------------------------------


def _blank_person():
    return {
        "visits": 0,
        "sources": [],
        "buckets": set(),
        "ua": "",
        "country": "",
        "signed_in": False,
        "engaged_event": False,
        "started": False,
        "seconds": 0.0,
        "max_pages": 0,
        "story_completions": set(),
    }


def _scan(days, caps):
    cutoff = get_cutoff(days)
    rows = audience_events(filter_since(AnalyticsEvent.objects.filter(event_type__in=SCAN_TYPES), cutoff)).values_list(
        "event_type", "user_id", "visitor_id", "session_id", "created_at", "story_id", "blog_id",
        "video_id", "duration_seconds", "metadata", "ua_family", "country_code",
    )
    people = {}
    sessions = {}  # (identity, session) -> entry page and what happened in it
    format_people = {name: set() for name in FORMATS}
    format_seconds = dict.fromkeys(FORMATS, 0.0)
    story_pairs = set()  # (identity, story) that started or spent time on a story
    story_done = set()  # (identity, story) that finished it, by any record
    other_completions = {"chapters": set(), "read_along": set(), "other": set()}

    for (
        event_type, user_id, visitor_id, session_id, created_at, story_id, blog_id, video_id, duration, metadata, ua, country
    ) in rows:
        identity = _content_identity(user_id, visitor_id)
        metadata = metadata or {}
        person = people.setdefault(identity, _blank_person())
        person["signed_in"] = person["signed_in"] or bool(user_id)
        local_date = timezone.localtime(created_at).date()
        skey = (identity, session_id or local_date)
        session = sessions.setdefault(
            skey, {"identity": identity, "pages": 0, "entry_at": None, "entry_path": None, "engaged": False, "times": []}
        )

        if event_type == AnalyticsEvent.EVENT_VISIT:
            person["visits"] += 1
            person["buckets"].add(python_time_bucket(created_at, days))
            person["sources"].append((created_at, metadata.get("referral_source")))
            if ua and not person["ua"]:
                person["ua"] = ua
            if country and not person["country"]:
                person["country"] = country
            session["pages"] += 1
            session["times"].append(created_at)
            if session["entry_at"] is None or created_at < session["entry_at"]:
                session["entry_at"], session["entry_path"] = created_at, metadata.get("path") or "unknown"
            person["max_pages"] = max(person["max_pages"], session["pages"])
            continue

        if event_type == AnalyticsEvent.EVENT_COMPLETION:
            content_type = metadata.get("content_type")
            item = (identity, story_id, metadata.get("item_slug"))
            if content_type == "story" and story_id:
                story_done.add((identity, story_id))
                person["story_completions"].add(story_id)
            elif content_type == "chapter":
                other_completions["chapters"].add(item)
            elif content_type == "read_along":
                other_completions["read_along"].add(item)
            else:
                other_completions["other"].add(item)
            continue
        if event_type == AnalyticsEvent.EVENT_STORY_COMPLETED:
            if story_id:
                story_done.add((identity, story_id))
                person["story_completions"].add(story_id)
            continue

        # Started a story, or spent time reading / listening / watching.
        person["engaged_event"] = True
        person["started"] = True
        session["engaged"] = True
        if story_id and not blog_id:
            story_pairs.add((identity, story_id))
        if event_type in SESSION_TYPES:
            quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and metadata.get("format") == "quick_read"
            seconds = caps.capped(duration, event_type, story_id=story_id, blog_id=blog_id, video_id=video_id, is_quick_read=quick)
            person["seconds"] += seconds
            if event_type == AnalyticsEvent.EVENT_LISTENING_SESSION:
                fmt = "listen"
            elif event_type == AnalyticsEvent.EVENT_WATCHING_SESSION:
                fmt = "watch"
            elif quick:
                fmt = "quick_read"
            elif blog_id:
                fmt = "blog"
            else:
                fmt = "read"
            format_people[fmt].add(identity)
            format_seconds[fmt] += seconds

    # Active browsing time: the gaps between page loads in a session, each capped.
    for session in sessions.values():
        times = sorted(session["times"])
        people[session["identity"]]["seconds"] += sum(
            min((later - earlier).total_seconds(), BROWSING_GAP_CAP_SECONDS) for earlier, later in zip(times, times[1:])
        )
    return {
        "cutoff": cutoff,
        "people": {identity: person for identity, person in people.items() if person["visits"]},
        "sessions": {key: s for key, s in sessions.items() if s["pages"]},
        "format_people": format_people,
        "format_seconds": format_seconds,
        "story_pairs": story_pairs,
        "story_done": story_done,
        "other_completions": other_completions,
    }


def _is_engaged(person):
    return (
        person["seconds"] >= ENGAGED_ACTIVE_SECONDS
        or person["max_pages"] >= ENGAGED_PAGE_VIEWS
        or person["engaged_event"]
    )


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _returning_before(cutoff):
    """Identities that visited before the window — they can't be new to it."""
    if cutoff is None:
        return set()
    earlier = audience_events(
        AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_VISIT, created_at__lt=cutoff)
    ).values_list("user_id", "visitor_id").distinct()
    return {_content_identity(user_id, visitor_id) for user_id, visitor_id in earlier}


def _build_traffic_quality(days, cutoff, sources):
    visits = audience_only(filter_since(AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_VISIT), cutoff))
    total = visits.count()
    flagged = visits.filter(is_suspected_bot=True).count()
    attributed = len(sources)
    known = sum(1 for source in sources.values() if source != "direct")
    return {
        "bot_share": _rate(flagged, total),
        "flagged_visit_events": flagged,
        "visit_events": total,
        # Of visitors we could attribute, how many arrived from somewhere we can name.
        "referrer_coverage": _rate(known, attributed),
        "direct_share": _rate(attributed - known, attributed),
    }


def build_audience_data(days):
    """One window of everything the Audience tab shows. Wrapped by
    build_audience_response(), which adds the previous period."""
    caps = SessionCaps()
    scan = _scan(days, caps)
    cutoff = scan["cutoff"]
    people = scan["people"]
    before = _returning_before(cutoff)

    total = len(people)
    engaged = {identity for identity, person in people.items() if _is_engaged(person)}
    returning = {identity for identity, person in people.items() if identity in before or len(person["buckets"]) >= 2}
    signed_in = sum(1 for person in people.values() if person["signed_in"])
    engaged_minutes = [people[identity]["seconds"] / 60 for identity in engaged]

    sources = first_touch_sources(
        (identity, created_at, raw) for identity, person in people.items() for created_at, raw in person["sources"]
    )

    # New vs returning visitors per bucket: returning on a bucket means seen
    # before the window, or in an earlier bucket of it.
    by_bucket = {}
    for identity, person in people.items():
        first = min(person["buckets"])
        for bucket in person["buckets"]:
            kind = "returning_visitors" if (identity in before or bucket > first) else "new_visitors"
            by_bucket.setdefault(bucket, {"new_visitors": set(), "returning_visitors": set()})[kind].add(identity)
    series = fill_time_buckets(
        [
            {"day": bucket, "new_visitors": len(kinds["new_visitors"]), "returning_visitors": len(kinds["returning_visitors"])}
            for bucket, kinds in by_bucket.items()
        ],
        days,
        defaults={"new_visitors": 0, "returning_visitors": 0},
    )

    acquisition = {}
    for identity, source in sources.items():
        person = people[identity]
        entry = acquisition.setdefault(source, {"visitors": 0, "engaged": 0, "starters": 0, "completions": 0, "returning": 0})
        entry["visitors"] += 1
        entry["engaged"] += identity in engaged
        entry["starters"] += person["started"]
        entry["completions"] += len(person["story_completions"])
        entry["returning"] += identity in returning

    # Landing pages: where each session began, and what happened next.
    landing = {}
    for session in scan["sessions"].values():
        entry = landing.setdefault(
            session["entry_path"], {"visitors": set(), "sessions": 0, "bounced": 0, "started": 0}
        )
        entry["visitors"].add(session["identity"])
        entry["sessions"] += 1
        entry["bounced"] += session["pages"] == 1 and not session["engaged"]
        entry["started"] += session["engaged"]

    devices = Counter(person["ua"] for person in people.values() if person["ua"])
    countries = Counter(person["country"] for person in people.values() if person["country"])
    starters = scan["story_pairs"]
    finished = len(scan["story_done"] & starters)
    other = scan["other_completions"]

    return {
        "range_days": days,
        "time_interval": get_time_interval(days),
        "traffic_quality": _build_traffic_quality(days, cutoff, sources),
        "kpis": {
            "unique_visitors": {"value": total, "sample_size": total},
            "engaged_visitors": {"value": len(engaged), "sample_size": total},
            "engaged_rate": {"value": _rate(len(engaged), total), "sample_size": total},
            "returning_rate": {"value": _rate(len(returning), total), "sample_size": total},
            "median_engaged_minutes": {
                "value": round(statistics.median(engaged_minutes), 1) if engaged_minutes else None,
                "sample_size": len(engaged_minutes),
            },
            "signed_in_share": {"value": _rate(signed_in, total), "sample_size": total},
        },
        "visitors_over_time": series,
        "publish_markers": build_publish_markers(days, cutoff, include_blogs=True),
        "acquisition": sorted(
            (
                {
                    "source": source,
                    "unique_visitors": entry["visitors"],
                    "engaged_visitors": entry["engaged"],
                    "engaged_rate": _rate(entry["engaged"], entry["visitors"]),
                    "unique_starters": entry["starters"],
                    "story_completions": entry["completions"],
                    "returning_visitors": entry["returning"],
                    "returning_rate": _rate(entry["returning"], entry["visitors"]),
                }
                for source, entry in acquisition.items()
            ),
            key=lambda row: (-row["unique_visitors"], row["source"]),
        ),
        "landing_pages_raw": sorted(
            (
                {
                    "path": path,
                    "unique_visitors": len(entry["visitors"]),
                    "sessions": entry["sessions"],
                    "bounce_rate": _rate(entry["bounced"], entry["sessions"]),
                    "started_rate": _rate(entry["started"], entry["sessions"]),
                }
                for path, entry in landing.items()
            ),
            key=lambda row: (-row["unique_visitors"], -row["sessions"], row["path"]),
        )[:LANDING_PAGE_LIMIT],
        "behaviour": {
            "format_split": [
                {
                    "format": fmt,
                    "unique_people": len(scan["format_people"][fmt]),
                    "minutes": round(scan["format_seconds"][fmt] / 60, 1),
                }
                for fmt in FORMATS
            ],
            "devices": [
                {"ua_family": family, "visitors": count}
                for family, count in sorted(devices.items(), key=lambda item: (-item[1], item[0]))
            ],
            "countries": [
                {"country_code": code, "visitors": count}
                for code, count in sorted(countries.items(), key=lambda item: (-item[1], item[0]))[:15]
            ],
            "completions": {
                # Whole stories finished (a story-level record, not a chapter).
                "stories": len(scan["story_done"]),
                "chapters": len(other["chapters"]),
                "read_along": len(other["read_along"]),
                "other": len(other["other"]),
                # Completion rate is stories only: of people who started one.
                "story_starters": len(starters),
                "story_completion_rate": _rate(finished, len(starters)) if len(starters) >= MIN_SAMPLE else None,
            },
        },
    }


def _with_minimum(entry, always_show):
    if entry is None:
        return None, 0
    shown = always_show or entry["sample_size"] >= MIN_SAMPLE
    return (entry["value"] if shown else None), entry["sample_size"]


def build_audience_response(days):
    """The Audience tab payload: the window and how it compares with the one before."""
    data = build_with_previous_period(build_audience_data, days)
    previous = data.pop("comparison")

    kpis = {}
    for key, entry in data["kpis"].items():
        always = key in ("unique_visitors", "engaged_visitors")  # plain counts
        value, sample = _with_minimum(entry, always)
        before, before_sample = _with_minimum(previous["kpis"][key] if previous else None, always)
        kpis[key] = {
            "value": value,
            "previous_value": before if previous else None,
            "sample_size": sample,
            "previous_sample_size": before_sample if previous else None,
            "min_sample": MIN_SAMPLE,
        }

    previous_by_source = {row["source"]: row["unique_visitors"] for row in previous["acquisition"]} if previous else {}
    acquisition = [
        {**row, "previous_unique_visitors": previous_by_source.get(row["source"], 0) if previous else None}
        for row in data["acquisition"]
    ]

    paths = resolve_paths(row["path"] for row in data["landing_pages_raw"])
    landing_pages = [
        {**row, **{k: v for k, v in paths[row["path"]].items() if k != "path"}} for row in data["landing_pages_raw"]
    ]

    quality = dict(data["traffic_quality"])
    quality["previous_bot_share"] = previous["traffic_quality"]["bot_share"] if previous else None
    last_run = BotFlagRun.objects.filter(applied=True).first()
    quality["last_bot_flag_run"] = (
        {
            "ran_at": last_run.ran_at,
            "flagged_events": last_run.flagged_events,
            "flagged_visitors": last_run.flagged_visitors,
            "rules": last_run.rules,
            "hours_ago": round((timezone.now() - last_run.ran_at).total_seconds() / 3600, 1),
        }
        if last_run
        else None
    )

    return {
        "range_days": days,
        "time_interval": data["time_interval"],
        "min_sample": MIN_SAMPLE,
        "traffic_quality": quality,
        "kpis": kpis,
        "visitors_over_time": {
            "series": data["visitors_over_time"],
            "previous_series": previous["visitors_over_time"] if previous else None,
            "publish_markers": data["publish_markers"],
        },
        "acquisition": acquisition,
        "landing_pages": landing_pages,
        "slug_warnings": build_slug_warnings(),
        "behaviour": data["behaviour"],
    }


def build_audience_export_data(days):
    """build_audience_response() as the flat tables the CSV/XLSX export is made
    of (see SECTION_TABLE_KEYS["audience"])."""
    data = build_audience_response(days)
    previous = data["visitors_over_time"]["previous_series"] or []
    return {
        "visitors_over_time": [
            {**row, "previous_period_total": (previous[index]["new_visitors"] + previous[index]["returning_visitors"]) if index < len(previous) else None}
            for index, row in enumerate(data["visitors_over_time"]["series"])
        ],
        "acquisition": data["acquisition"],
        "landing_pages": [{k: v for k, v in row.items() if k != "resolved"} for row in data["landing_pages"]],
        "devices": data["behaviour"]["devices"],
        "countries": data["behaviour"]["countries"],
        "format_split": data["behaviour"]["format_split"],
        **{f"kpi_{key}": kpi for key, kpi in data["kpis"].items()},
        "traffic_quality": {k: v for k, v in data["traffic_quality"].items() if k != "last_bot_flag_run"},
        "completions": data["behaviour"]["completions"],
    }


class AdminAnalyticsAudienceAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_audience_response(get_range_days(request)))


# --- Monetization ------------------------------------------------------------------


def build_monetization_data(days):
    """Ad impressions and downloads for one window."""
    cutoff = get_cutoff(days)
    events = audience_events(filter_since(AnalyticsEvent.objects.all(), cutoff))
    ads = events.filter(event_type=AnalyticsEvent.EVENT_AD_IMPRESSION)
    downloads = events.filter(event_type=AnalyticsEvent.EVENT_DOWNLOAD)
    downloaders = {
        _content_identity(user_id, visitor_id) for user_id, visitor_id in downloads.values_list("user_id", "visitor_id")
    }
    return {
        "ad_impressions": ads.count(),
        "downloads": downloads.count(),
        "unique_downloaders": len(downloaders),
        "ad_placements": list(
            ads.values("metadata__path", "metadata__size").annotate(count=Count("id")).order_by("-count")[:12]
        ),
        "ad_by_content_type": list(
            ads.values("metadata__content_type").annotate(count=Count("id")).order_by("-count")
        ),
        "download_types": list(
            downloads.values("metadata__content_type")
            .annotate(count=Count("id"), bytes=Sum("value"))
            .order_by("-count")
        ),
        "top_downloads": list(
            downloads.filter(story_id__isnull=False)
            .values("story_id", "story__title", "story__slug")
            .annotate(count=Count("id"), bytes=Sum("value"))
            .order_by("-count", "story__title")[:10]
        ),
    }


def _kpi(value, previous):
    change = round((value - previous) / previous * 100, 1) if previous else None
    return {"value": value, "previous_value": previous, "change_pct": change}


def build_monetization_response(days):
    data = build_with_previous_period(build_monetization_data, days)
    previous = data.pop("comparison")
    paths = resolve_paths(row["metadata__path"] for row in data["ad_placements"])
    return {
        "range_days": days,
        "has_data": bool(data["ad_impressions"] or data["downloads"]),
        "kpis": {
            key: _kpi(data[key], previous[key] if previous else None)
            for key in ("ad_impressions", "downloads", "unique_downloaders")
        },
        "ad_placements": [
            {
                "path": row["metadata__path"] or "unknown",
                "title": paths[row["metadata__path"]]["title"] if row["metadata__path"] else "Unknown page",
                "type": paths[row["metadata__path"]]["type"] if row["metadata__path"] else "other",
                "size": row["metadata__size"] or "unknown",
                "count": row["count"],
            }
            for row in data["ad_placements"]
        ],
        "ad_by_content_type": [
            {"content_type": row["metadata__content_type"] or "unknown", "count": row["count"]}
            for row in data["ad_by_content_type"]
        ],
        "download_types": [
            {"content_type": row["metadata__content_type"] or "unknown", "count": row["count"], "bytes": round(row["bytes"] or 0)}
            for row in data["download_types"]
        ],
        "top_downloads": [
            {
                "story_id": row["story_id"],
                "title": row["story__title"],
                "slug": row["story__slug"],
                "count": row["count"],
                "bytes": round(row["bytes"] or 0),
            }
            for row in data["top_downloads"]
        ],
    }


class AdminAnalyticsMonetizationAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        return Response(build_monetization_response(get_range_days(request)))

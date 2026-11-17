"""Analytics -> Geography tab.

Two scopes:

* `visitors` (default): every audience visitor, anonymous included, placed by
  the `country_code` stored on their browser events (Cloudflare's CF-IPCountry,
  see apps/stats/request_meta.py).
* `signed_in`: the original view — only people who logged in, from
  UserLoginLocation (build_geography_data in analytics_api, unchanged).

Rules shared with the Audience tab: staff and suspected bots are dropped, a
"visitor" is the signed-in user else the browser's visitor_id
(_content_identity), minutes use SessionCaps, and "engaged" means 30 s of
activity, 2 page views in a session or any reading/listening/watching.

A visitor's country is the first one seen on any of their browser events. Events
the server raises itself (visitor_id "server") carry no country and are left out
of coverage.
"""

from collections import Counter, defaultdict

from django.db.models import Count, Min, Q
from django.http import Http404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.stats.models import AnalyticsEvent

from .analytics_api import (
    CACHE_SECONDS,
    _bucket_floor,
    _bucket_key,
    _content_identity,
    _next_bucket,
    audience_events,
    audience_only,
    build_geography_data,
    build_referral_sources,
    build_with_previous_period,
    filter_since,
    get_cutoff,
    get_range_days,
    first_touch_sources,
)
from .api import IsSuperUser
from .audience_api import (
    BROWSING_GAP_CAP_SECONDS,
    ENGAGED_ACTIVE_SECONDS,
    ENGAGED_PAGE_VIEWS,
    MIN_SAMPLE,
    SCAN_TYPES,
    SESSION_TYPES,
    STARTED_TYPES,
    resolve_paths,
)
from .content_scoring import SessionCaps
from .models import COUNTRY_CHOICES, LANGUAGE_CHOICES, Story

COUNTRY_NAMES = dict(COUNTRY_CHOICES)
LANGUAGE_NAMES = dict(LANGUAGE_CHOICES)
TOP_COUNTRIES_IN_CHART = 5
CITY_LIMIT = 20
DETAIL_LIMIT = 10


def country_name(code):
    return COUNTRY_NAMES.get(code, code)


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def geo_interval(days):
    """Bucket size for the over-time chart: weekly, except daily for a week and
    hourly for a day (monthly for a year)."""
    if days == 1:
        return "hour"
    if days == 7:
        return "day"
    if days in (30, 90):
        return "week"
    return "month"


def _geo_bucket(moment, interval):
    return _bucket_key(_bucket_floor(moment, interval), interval, False)


def _geo_bucket_keys(days, interval):
    keys, cursor = [], _bucket_floor(get_cutoff(days), interval)
    last = _bucket_floor(timezone.now(), interval)
    while cursor <= last:
        keys.append(_bucket_key(cursor, interval, False))
        cursor = _next_bucket(cursor, interval)
    return keys


# --- the scan ---------------------------------------------------------------


def _blank_person():
    return {
        "country": "",
        "signed_in": False,
        "visits": 0,
        "buckets": set(),
        "ua": "",
        "sources": [],
        "engaged_event": False,
        "starter": False,
        "seconds": 0.0,  # engagement time: capped sessions + browsing gaps
        "session_seconds": 0.0,  # reading / listening / watching only
        "max_pages": 0,
        "stories": {},  # story_id -> {"seconds", "started"}
        "formats": defaultdict(float),
        "completions": set(),
        "entry_paths": [],
    }


def _scan(days, caps):
    """Every audience visitor in the window, with what they did."""
    interval = geo_interval(days)
    cutoff = get_cutoff(days)
    rows = (
        audience_events(filter_since(AnalyticsEvent.objects.filter(event_type__in=SCAN_TYPES), cutoff))
        .order_by("created_at")
        .values_list(
            "event_type", "user_id", "visitor_id", "session_id", "created_at", "story_id", "blog_id",
            "video_id", "duration_seconds", "metadata", "ua_family", "country_code",
        )
    )
    people, sessions = {}, {}
    for event_type, user_id, visitor_id, session_id, created_at, story_id, blog_id, video_id, duration, metadata, ua, country in rows:
        identity = _content_identity(user_id, visitor_id)
        metadata = metadata or {}
        person = people.setdefault(identity, _blank_person())
        person["signed_in"] = person["signed_in"] or bool(user_id)
        if country and not person["country"]:
            person["country"] = country
        skey = (identity, session_id or timezone.localtime(created_at).date())
        session = sessions.setdefault(skey, {"identity": identity, "pages": 0, "entry_at": None, "entry_path": None, "times": []})

        if event_type == AnalyticsEvent.EVENT_VISIT:
            person["visits"] += 1
            person["buckets"].add(_geo_bucket(created_at, interval))
            person["sources"].append((created_at, metadata.get("referral_source")))
            if ua and not person["ua"]:
                person["ua"] = ua
            session["pages"] += 1
            session["times"].append(created_at)
            if session["entry_at"] is None or created_at < session["entry_at"]:
                session["entry_at"], session["entry_path"] = created_at, metadata.get("path") or "unknown"
            person["max_pages"] = max(person["max_pages"], session["pages"])
            if story_id and not blog_id:
                person["stories"].setdefault(story_id, {"seconds": 0.0, "started": False})
            continue

        if event_type in (AnalyticsEvent.EVENT_COMPLETION, AnalyticsEvent.EVENT_STORY_COMPLETED):
            is_story = event_type == AnalyticsEvent.EVENT_STORY_COMPLETED or metadata.get("content_type") == "story"
            if is_story and story_id:
                person["completions"].add(story_id)
            continue

        person["engaged_event"] = True
        entry = person["stories"].setdefault(story_id, {"seconds": 0.0, "started": False}) if story_id and not blog_id else None
        if event_type in STARTED_TYPES:
            person["starter"] = True
            if entry is not None:
                entry["started"] = True
        if event_type in SESSION_TYPES:
            quick = event_type == AnalyticsEvent.EVENT_READING_SESSION and metadata.get("format") == "quick_read"
            seconds = caps.capped(duration, event_type, story_id=story_id, blog_id=blog_id, video_id=video_id, is_quick_read=quick)
            person["seconds"] += seconds
            person["session_seconds"] += seconds
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
            person["formats"][fmt] += seconds
            if entry is not None:
                entry["seconds"] += seconds

    for session in sessions.values():
        person = people[session["identity"]]
        times = sorted(session["times"])
        person["seconds"] += sum(
            min((later - earlier).total_seconds(), BROWSING_GAP_CAP_SECONDS) for earlier, later in zip(times, times[1:])
        )
        if session["entry_path"]:
            person["entry_paths"].append(session["entry_path"])
    return {"interval": interval, "people": {k: v for k, v in people.items() if v["visits"]}}


def _is_engaged(person):
    return (
        person["seconds"] >= ENGAGED_ACTIVE_SECONDS
        or person["max_pages"] >= ENGAGED_PAGE_VIEWS
        or person["engaged_event"]
    )


def _top_story_ids(people):
    """{story_id: (visitors, readers, seconds)} across the given people."""
    stats = defaultdict(lambda: [0, 0, 0.0])
    for person in people:
        for story_id, entry in person["stories"].items():
            row = stats[story_id]
            row[0] += 1
            row[1] += 1 if entry["started"] else 0
            row[2] += entry["seconds"]
    return stats


def _bot_counts(cutoff):
    """{country_code: (visit events, suspected-bot visit events)} — staff are
    out but bots are deliberately still in, so the share is of everything that
    actually arrived."""
    visits = audience_only(
        filter_since(AnalyticsEvent.objects.filter(event_type=AnalyticsEvent.EVENT_VISIT), cutoff)
    ).exclude(visitor_id=AnalyticsEvent.SERVER_VISITOR_ID)
    return {
        row["country_code"]: (row["total"], row["flagged"])
        for row in visits.values("country_code").annotate(
            total=Count("id"), flagged=Count("id", filter=Q(is_suspected_bot=True))
        )
    }


def _coverage(cutoff):
    browser = audience_events(filter_since(AnalyticsEvent.objects.all(), cutoff)).exclude(
        visitor_id=AnalyticsEvent.SERVER_VISITOR_ID
    )
    total = browser.count()
    return total, browser.exclude(country_code="").count()


def build_visitors_geography(days):
    """One window of the visitors-scope Geography tab. Wrapped by
    build_geography_response(), which adds the previous period."""
    cutoff = get_cutoff(days)
    scan = _scan(days, SessionCaps())
    people = scan["people"]
    interval = scan["interval"]

    by_code = defaultdict(list)
    for identity, person in people.items():
        if person["country"]:
            by_code[person["country"]].append((identity, person))

    bot_counts = _bot_counts(cutoff)
    all_story_ids = set()
    rows = []
    for code, group in by_code.items():
        members = [p for _i, p in group]
        stories = _top_story_ids(members)
        top_story_id = max(stories, key=lambda sid: (stories[sid][0], stories[sid][2], -sid), default=None)
        if top_story_id:
            all_story_ids.add(top_story_id)
        format_people, format_seconds = Counter(), Counter()
        for person in members:
            for fmt, seconds in person["formats"].items():
                format_people[fmt] += 1
                format_seconds[fmt] += seconds
        top_format = max(format_people, key=lambda f: (format_people[f], format_seconds[f], f), default=None)
        sources = first_touch_sources((i, at, raw) for i, p in group for at, raw in p["sources"])
        source_counts = Counter(sources.values())
        top_source = max(source_counts, key=lambda s: (source_counts[s], s), default=None)
        engaged = sum(1 for p in members if _is_engaged(p))
        total_visits, flagged = bot_counts.get(code, (0, 0))
        rows.append(
            {
                "country_code": code,
                "country": country_name(code),
                "visitors": len(members),
                "engaged_visitors": engaged,
                "engaged_rate": _rate(engaged, len(members)),
                "readers": sum(1 for p in members if p["starter"]),
                "minutes": round(sum(p["session_seconds"] for p in members) / 60, 1),
                "completions": sum(len(p["completions"]) for p in members),
                "signed_in_users": sum(1 for p in members if p["signed_in"]),
                "top_story": top_story_id,
                "top_format": top_format,
                "top_source": top_source,
                "bot_share": _rate(flagged, total_visits),
                "bot_visit_events": total_visits,
            }
        )
    titles = {
        pk: {"id": pk, "title": title, "slug": slug}
        for pk, title, slug in Story.objects.filter(pk__in=all_story_ids).values_list("id", "title", "slug")
    }
    for row in rows:
        row["top_story"] = titles.get(row["top_story"])
    rows.sort(key=lambda row: (-row["visitors"], row["country_code"]))

    total_located = sum(row["visitors"] for row in rows)
    top = rows[0] if rows else None
    total_visits = sum(total for total, _flagged in bot_counts.values())
    flagged_visits = sum(flagged for _total, flagged in bot_counts.values())
    browser_events, located_events = _coverage(cutoff)

    # Top countries over time: unique visitors per bucket, top 5 + Other.
    chart_codes = [row["country_code"] for row in rows[:TOP_COUNTRIES_IN_CHART]]
    series_keys = chart_codes + (["other"] if len(rows) > TOP_COUNTRIES_IN_CHART else [])
    per_bucket = defaultdict(lambda: defaultdict(set))
    for identity, person in people.items():
        if not person["country"]:
            continue
        key = person["country"] if person["country"] in chart_codes else "other"
        for bucket in person["buckets"]:
            per_bucket[bucket][key].add(identity)
    bucket_keys = _geo_bucket_keys(days, interval)
    over_time = [
        {"day": bucket, **{key: len(per_bucket[bucket].get(key, ())) for key in series_keys}}
        for bucket in sorted(set(bucket_keys) | set(per_bucket))
    ]

    # Cities: from events that carry one; otherwise the signed-in logins.
    city_rows = (
        audience_events(filter_since(AnalyticsEvent.objects.exclude(city=""), cutoff))
        .exclude(visitor_id=AnalyticsEvent.SERVER_VISITOR_ID)
        .values_list("user_id", "visitor_id", "country_code", "city", "region")
    )
    city_people = defaultdict(set)
    for user_id, visitor_id, code, city, region in city_rows:
        city_people[(code, city, region)].add(_content_identity(user_id, visitor_id))
    if city_people:
        cities_source = "events"
        cities = [
            {"city": city, "region": region, "country_code": code, "country": country_name(code), "visitors": len(ids)}
            for (code, city, region), ids in city_people.items()
        ]
        cities.sort(key=lambda c: (-c["visitors"], c["city"]))
        cities = cities[:CITY_LIMIT]
    else:
        cities_source = "signed_in_only"
        cities = [
            {"city": c["city"], "region": "", "country_code": "", "country": c["country"], "visitors": c["users"]}
            for c in build_geography_data(days)["by_city"]
        ]

    return {
        "range_days": days,
        "time_interval": interval,
        "located_visitors": total_located,
        "total_visitors": len(people),
        "top_country": (
            {"country_code": top["country_code"], "country": top["country"], "visitors": top["visitors"], "share": _rate(top["visitors"], total_located)}
            if top
            else None
        ),
        "outside_top_share": _rate(total_located - top["visitors"], total_located) if top else None,
        "countries_reached": len(rows),
        "coverage": _rate(located_events, browser_events),
        "browser_events": browser_events,
        "bot_share": _rate(flagged_visits, total_visits),
        "visit_events": total_visits,
        "by_country": rows,
        "series": [{"key": key, "label": "Other" if key == "other" else country_name(key)} for key in series_keys],
        "over_time": over_time,
        "cities": cities,
        "cities_source": cities_source,
    }


def _kpi(value, previous, sample, previous_sample, *, always=False):
    shown = value if always or sample >= MIN_SAMPLE else None
    has_previous = previous_sample is not None
    before = previous if has_previous and (always or previous_sample >= MIN_SAMPLE) else None
    return {
        "value": shown,
        "previous_value": before,
        "sample_size": sample,
        "previous_sample_size": previous_sample,
        "min_sample": MIN_SAMPLE,
    }


def build_geography_response(days):
    data = build_with_previous_period(build_visitors_geography, days)
    previous = data.pop("comparison")
    p = previous or {}

    def pair(key, sample_key, *, always=False):
        return _kpi(
            data[key], p.get(key), data[sample_key],
            p.get(sample_key) if previous else None, always=always,
        )

    top = data["top_country"]
    previous_top = p.get("top_country")
    kpis = {
        "countries_reached": pair("countries_reached", "countries_reached", always=True),
        "top_country": {
            "value": top,
            "previous_value": previous_top if previous else None,
            "sample_size": data["located_visitors"],
            "previous_sample_size": p.get("located_visitors") if previous else None,
            "min_sample": MIN_SAMPLE,
        },
        "outside_top_country_share": pair("outside_top_share", "located_visitors"),
        "country_coverage": pair("coverage", "browser_events"),
        "bot_share": pair("bot_share", "visit_events"),
    }
    previous_visitors = {row["country_code"]: row["visitors"] for row in p.get("by_country", [])} if previous else {}
    by_country = [
        {**row, "previous_visitors": previous_visitors.get(row["country_code"], 0) if previous else None}
        for row in data["by_country"]
    ]
    first_located = AnalyticsEvent.objects.exclude(country_code="").aggregate(first=Min("created_at"))["first"]
    return {
        "scope": "visitors",
        "range_days": days,
        "time_interval": data["time_interval"],
        "min_sample": MIN_SAMPLE,
        "kpis": kpis,
        "by_country": by_country,
        "series": data["series"],
        "over_time": data["over_time"],
        "cities": data["cities"],
        "cities_source": data["cities_source"],
        "data_since": first_located,
        "has_previous": previous is not None,
    }


def build_country_detail(code, days):
    """What one country's visitors read, where they land, how they arrive."""
    scan = _scan(days, SessionCaps())
    group = [(identity, p) for identity, p in scan["people"].items() if p["country"] == code]
    members = [p for _i, p in group]

    stories = _top_story_ids(members)
    ranked = sorted(stories, key=lambda sid: (-stories[sid][0], -stories[sid][2], sid))[:DETAIL_LIMIT]
    story_rows = {pk: (title, slug, language) for pk, title, slug, language in Story.objects.filter(pk__in=stories).values_list("id", "title", "slug", "language")}
    top_stories = [
        {
            "id": sid, "title": story_rows[sid][0], "slug": story_rows[sid][1],
            "visitors": stories[sid][0], "readers": stories[sid][1], "minutes": round(stories[sid][2] / 60, 1),
        }
        for sid in ranked
        if sid in story_rows
    ]

    landing = Counter()
    for identity, person in group:
        for path in set(person["entry_paths"]):
            landing[path] += 1
    top_paths = [path for path, _n in sorted(landing.items(), key=lambda kv: (-kv[1], kv[0]))[:DETAIL_LIMIT]]
    resolved = resolve_paths(top_paths)
    landing_pages = [{**{k: v for k, v in resolved[path].items() if k != "path"}, "path": path, "visitors": landing[path]} for path in top_paths]

    sources = first_touch_sources((i, at, raw) for i, p in group for at, raw in p["sources"])
    referral_sources = [
        {"referral_source": source, "count": count}
        for source, count in sorted(Counter(sources.values()).items(), key=lambda kv: (-kv[1], kv[0]))
    ][:DETAIL_LIMIT]

    devices = Counter(p["ua"] for p in members if p["ua"])
    language_people = Counter()
    for person in members:
        for language in {story_rows[sid][2] for sid, entry in person["stories"].items() if entry["started"] and sid in story_rows}:
            language_people[language] += 1

    return {
        "country_code": code,
        "country": country_name(code),
        "range_days": days,
        "visitors": len(members),
        "top_stories": top_stories,
        "landing_pages": landing_pages,
        "referral_sources": referral_sources,
        "devices": [{"ua_family": ua, "visitors": n} for ua, n in sorted(devices.items(), key=lambda kv: (-kv[1], kv[0]))],
        "languages": [
            {"language": language, "name": LANGUAGE_NAMES.get(language, language), "readers": n}
            for language, n in sorted(language_people.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }


class AdminAnalyticsGeographyAPIView(APIView):
    """Where the audience is. `?scope=visitors` (default) covers everyone with a
    country on their browser events, anonymous included; `?scope=signed_in` is
    the original login-location view, with its original keys."""

    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        days = get_range_days(request)
        if request.query_params.get("scope") == "signed_in":
            data = build_with_previous_period(build_geography_data, days)
            data["scope"] = "signed_in"
            return Response(data)
        return Response(build_geography_response(days))


class AdminAnalyticsGeographyCountryAPIView(APIView):
    permission_classes = [IsSuperUser]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request, country_code):
        code = (country_code or "").strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise Http404
        return Response(build_country_detail(code, get_range_days(request)))

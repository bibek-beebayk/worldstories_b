import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.stats.models import AnalyticsEvent

# A read-only report for investigating "is this spike bots?". For each day it
# prints the signals that separate a crawler farm from real readers: how many
# visitors did nothing but load one page, how many arrived with no referrer, and
# how concentrated the traffic is on one browser family, country or IP.
#
#     python manage.py audit_traffic --from 2026-10-10 --to 2026-10-25
#
# Days well above the median that are also mostly single-page, mostly direct and
# concentrated are bots (flag_suspected_bots --apply will flag the ones that
# meet its rules). A spike that arrives with a social referrer and a spread of
# browsers and countries is real traffic.


def _parse(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not a YYYY-MM-DD date.") from exc


def _top(counter, total):
    if not counter or not total:
        return "-", 0.0
    name, count = counter.most_common(1)[0]
    return name, count / total


class Command(BaseCommand):
    help = "Per-day traffic-quality report (single-page visitors, direct share, concentration) to investigate spikes."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", help="First day, YYYY-MM-DD (default: --days ago).")
        parser.add_argument("--to", dest="end", help="Last day, YYYY-MM-DD (default: today).")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--include-flagged", action="store_true", help="Count rows already flagged as bots.")

    def handle(self, *args, **options):
        end = _parse(options["end"]) if options["end"] else timezone.localdate()
        start = _parse(options["start"]) if options["start"] else end - timedelta(days=options["days"])
        if start > end:
            raise CommandError("--from must not be after --to.")
        window_start = timezone.make_aware(datetime.combine(start, time.min))
        window_end = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min))

        rows = AnalyticsEvent.objects.filter(
            created_at__gte=window_start, created_at__lt=window_end, user__isnull=True
        )
        if not options["include_flagged"]:
            rows = rows.filter(is_suspected_bot=False)

        days = defaultdict(lambda: {"events": Counter(), "ua": Counter(), "country": Counter(), "ip": Counter(), "direct": 0, "visits": 0, "flagged": 0, "paths": Counter()})
        per_visitor = defaultdict(lambda: defaultdict(lambda: {"visits": 0, "other": 0}))
        for event_type, visitor_id, created_at, ua, country, ip_hash, flagged, metadata in rows.values_list(
            "event_type", "visitor_id", "created_at", "ua_family", "country_code", "ip_hash", "is_suspected_bot", "metadata"
        ):
            day = timezone.localtime(created_at).date()
            entry = days[day]
            entry["events"][event_type] += 1
            slot = per_visitor[day][visitor_id]
            if event_type == AnalyticsEvent.EVENT_VISIT:
                slot["visits"] += 1
                entry["visits"] += 1
                metadata = metadata or {}
                if (metadata.get("referral_source") or "direct").strip().lower() == "direct":
                    entry["direct"] += 1
                if metadata.get("path"):
                    entry["paths"][metadata["path"]] += 1
                entry["ua"][ua or "(none)"] += 1
                entry["country"][country or "(none)"] += 1
                if ip_hash:
                    entry["ip"][ip_hash[:8]] += 1
            else:
                slot["other"] += 1
            if flagged:
                entry["flagged"] += 1

        if not days:
            self.stdout.write("No anonymous events in that window.")
            return

        visit_counts = [entry["visits"] for entry in days.values()]
        median_visits = statistics.median(visit_counts)
        self.stdout.write(f"Anonymous visits per day, {start} to {end} (median {median_visits:.0f}). * = above 2x the median.\n")
        header = f"{'day':10} {'visits':>6} {'visitors':>8} {'pg/vis':>6} {'1-page%':>7} {'engaged%':>8} {'direct%':>7}  {'top browser':22} {'top country':12} {'top ip':>6} {'flagged':>7}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for day in sorted(days):
            entry = days[day]
            visitors = per_visitor[day]
            total = len(visitors)
            single = sum(1 for slot in visitors.values() if slot["visits"] == 1 and slot["other"] == 0)
            engaged = sum(1 for slot in visitors.values() if slot["other"] > 0)
            ua_name, ua_share = _top(entry["ua"], entry["visits"])
            country_name, country_share = _top(entry["country"], entry["visits"])
            _ip, ip_share = _top(entry["ip"], entry["visits"])
            marker = "*" if median_visits and entry["visits"] > 2 * median_visits else " "
            self.stdout.write(
                f"{day} {entry['visits']:>6}{marker}{total:>7} "
                f"{(entry['visits'] / total if total else 0):>6.1f} {(single / total if total else 0):>7.0%} "
                f"{(engaged / total if total else 0):>8.0%} {(entry['direct'] / entry['visits'] if entry['visits'] else 0):>7.0%}  "
                f"{ua_name[:16] + f' {ua_share:.0%}':22} {country_name[:6] + f' {country_share:.0%}':12} {ip_share:>6.0%} {entry['flagged']:>7}"
            )
        spikes = [day for day in sorted(days) if median_visits and days[day]["visits"] > 2 * median_visits]
        for day in spikes:
            paths = days[day]["paths"].most_common(5)
            self.stdout.write(f"\nSpike {day}: top landing paths: " + ", ".join(f"{path} ({count})" for path, count in paths))

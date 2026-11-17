import statistics
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.stats.models import AnalyticsEvent, BotFlagRun

# The ingest UA gate only catches bots that announce themselves. This finds
# the ones that don't. Two rules, each deliberately conservative:
#
#   ip_burst  A single IP producing many distinct visitor_ids in a day, none of
#             which ever did anything but load a page.
#   spike     A day on which "single-hit" visitors (one page view, nothing else,
#             ever) jump to several times their usual level AND are concentrated
#             (one browser family / country / IP) AND arrive with no referrer.
#             That is what a crawler farm with a headless browser looks like;
#             a viral post is *also* a spike of single-hit visitors, but it
#             arrives with a referrer from a social network, which is why the
#             direct-share requirement is not optional.
#
# Caution: many real users can share one IP on carrier-grade NAT (mobile
# carriers, campus and office networks). That is why ip_burst requires ZERO
# engagement from every visitor behind the IP-day — a shared IP full of
# genuine readers always has reading/listening/completion/download events —
# and why this defaults to a dry run. Nothing is deleted; flagged rows are
# only excluded from the dashboard's audience metrics, and --unflag reverses it.
#
# Nothing in the repo schedules this (the backend is one container running
# gunicorn, no worker or scheduler), so it must be run from the host's cron:
#     python manage.py flag_suspected_bots --apply
# Every run is logged to BotFlagRun, which the Audience tab reads to warn when
# it hasn't run for a day.

ALL_RULES = ("ip_burst", "spike")


def _candidate_groups(cutoff, min_visitors):
    """Yield (ip_hash, day, visitor_ids) for each zero-engagement IP-day whose
    distinct-visitor count exceeds min_visitors. Rows with a user are never
    considered, so a logged-in account can't be swept up."""
    anonymous = AnalyticsEvent.objects.filter(
        created_at__gte=cutoff, user__isnull=True
    ).exclude(ip_hash="")
    groups = (
        anonymous.annotate(day=TruncDate("created_at"))
        .values("ip_hash", "day")
        .annotate(visitors=Count("visitor_id", distinct=True))
        .filter(visitors__gt=min_visitors)
    )
    for group in groups:
        visitor_ids = set(
            anonymous.annotate(day=TruncDate("created_at"))
            .filter(ip_hash=group["ip_hash"], day=group["day"])
            .values_list("visitor_id", flat=True)
        )
        engaged = (
            AnalyticsEvent.objects.filter(visitor_id__in=visitor_ids)
            .exclude(event_type=AnalyticsEvent.EVENT_VISIT)
            .exists()
        )
        if not engaged:
            yield group["ip_hash"], group["day"], visitor_ids


def _top_share(values):
    """The share of the most common non-blank value among `values`."""
    counted = Counter(value for value in values if value)
    total = len(values)
    return (counted.most_common(1)[0][1] / total) if counted and total else 0.0


def _spike_groups(cutoff, *, factor, min_visitors, concentration, min_direct_share, baseline_days):
    """Yield (day, visitor_ids, detail) for each day whose single-hit anonymous
    visitors spiked, concentrated, and arrived without a referrer."""
    lookback = cutoff - timedelta(days=baseline_days)
    # One row per anonymous visitor: how many events they have ever produced
    # (in the lookback window) and how many of those were not page views.
    per_visitor = (
        AnalyticsEvent.objects.filter(created_at__gte=lookback, user__isnull=True)
        .values("visitor_id")
        .annotate(
            events=Count("id"),
            engaged=Count("id", filter=~Q(event_type=AnalyticsEvent.EVENT_VISIT)),
            first=Min("created_at"),
        )
    )
    single_hit = {}
    for row in per_visitor:
        if row["events"] == 1 and row["engaged"] == 0:
            single_hit.setdefault(timezone.localtime(row["first"]).date(), set()).add(row["visitor_id"])

    first_day = timezone.localtime(cutoff).date()
    last_day = timezone.localdate()
    day = first_day
    while day <= last_day:
        visitors = single_hit.get(day, set())
        baseline_counts = [len(single_hit.get(day - timedelta(days=offset), ())) for offset in range(1, baseline_days + 1)]
        baseline = max(statistics.median(baseline_counts), 10)
        if len(visitors) >= min_visitors and len(visitors) > factor * baseline:
            rows = list(
                AnalyticsEvent.objects.filter(visitor_id__in=visitors, user__isnull=True).values_list(
                    "ua_family", "country_code", "ip_hash", "metadata"
                )
            )
            direct = sum(
                1 for *_rest, metadata in rows if ((metadata or {}).get("referral_source") or "direct").strip().lower() == "direct"
            )
            direct_share = direct / len(rows)
            shares = {
                "ua_family": _top_share([r[0] for r in rows]),
                "country": _top_share([r[1] for r in rows]),
                "ip_hash": _top_share([r[2] for r in rows]),
            }
            if direct_share >= min_direct_share and max(shares.values()) >= concentration:
                yield day, visitors, {"baseline": baseline, "direct_share": direct_share, **shares}
        day += timedelta(days=1)


class Command(BaseCommand):
    help = (
        "Flag AnalyticsEvent rows that look like undeclared bots (see the module "
        "comment for the rules). Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--min-visitors-per-ip", type=int, default=15)
        parser.add_argument(
            "--rules",
            default=",".join(ALL_RULES),
            help=f"Comma-separated rules to run, from: {', '.join(ALL_RULES)}.",
        )
        parser.add_argument("--spike-factor", type=float, default=3.0, help="Single-hit visitors vs their median day.")
        parser.add_argument("--min-spike-visitors", type=int, default=50)
        parser.add_argument("--spike-concentration", type=float, default=0.6, help="Share sharing one browser family, country or IP.")
        parser.add_argument("--min-direct-share", type=float, default=0.9, help="Share arriving with no referrer.")
        parser.add_argument("--baseline-days", type=int, default=14)
        parser.add_argument("--apply", action="store_true", help="Write the flags (default is a dry run).")
        parser.add_argument("--unflag", action="store_true", help="Clear flags in the window instead of setting them.")

    def handle(self, *args, **options):
        days = options["days"]
        threshold = options["min_visitors_per_ip"]
        if days < 1 or threshold < 1:
            raise CommandError("--days and --min-visitors-per-ip must be at least 1.")
        rules = [rule.strip() for rule in options["rules"].split(",") if rule.strip()]
        unknown = [rule for rule in rules if rule not in ALL_RULES]
        if unknown or not rules:
            raise CommandError(f"--rules must be a comma-separated subset of: {', '.join(ALL_RULES)}.")
        apply = options["apply"]
        cutoff = timezone.now() - timedelta(days=days)
        mode = "APPLY" if apply else "DRY RUN"

        if options["unflag"]:
            flagged = AnalyticsEvent.objects.filter(created_at__gte=cutoff, is_suspected_bot=True)
            count = flagged.count()
            if apply:
                flagged.update(is_suspected_bot=False)
            self.stdout.write(f"[{mode}] {'Unflagged' if apply else 'Would unflag'} {count} events in the last {days} days.")
            return

        events_by_ip = Counter()
        visitors = set()
        ip_hashes = set()
        event_ids = []

        def collect(day, visitor_ids, ip_hash=None):
            """Add one rule's visitors for one day to the run's totals."""
            rows = AnalyticsEvent.objects.filter(
                created_at__gte=cutoff, user__isnull=True, visitor_id__in=visitor_ids, is_suspected_bot=False
            )
            if ip_hash is not None:
                rows = rows.filter(ip_hash=ip_hash)
            rows = rows.annotate(day=TruncDate("created_at")).filter(day=day)
            ids = [pk for pk in rows.values_list("id", flat=True) if pk not in seen]
            seen.update(ids)
            visitors.update(visitor_ids)
            event_ids.extend(ids)
            return ids

        seen = set()
        if "ip_burst" in rules:
            for ip_hash, day, visitor_ids in _candidate_groups(cutoff, threshold):
                ids = collect(day, visitor_ids, ip_hash)
                ip_hashes.add(ip_hash)
                events_by_ip[ip_hash] += len(ids)

        spikes = []
        if "spike" in rules:
            for day, visitor_ids, detail in _spike_groups(
                cutoff,
                factor=options["spike_factor"],
                min_visitors=options["min_spike_visitors"],
                concentration=options["spike_concentration"],
                min_direct_share=options["min_direct_share"],
                baseline_days=options["baseline_days"],
            ):
                ids = collect(day, visitor_ids)
                for ip_hash in AnalyticsEvent.objects.filter(id__in=ids).exclude(ip_hash="").values_list("ip_hash", flat=True).distinct():
                    ip_hashes.add(ip_hash)
                spikes.append((day, len(visitor_ids), detail))

        if apply and event_ids:
            for start in range(0, len(event_ids), 5000):
                AnalyticsEvent.objects.filter(id__in=event_ids[start : start + 5000]).update(is_suspected_bot=True)
        BotFlagRun.objects.create(
            applied=apply,
            days=min(days, 32767),
            rules=",".join(rules),
            flagged_events=len(event_ids),
            flagged_visitors=len(visitors),
        )

        verb = "Flagged" if apply else "Would flag"
        self.stdout.write(
            f"[{mode}] {verb} {len(event_ids)} events from {len(visitors)} visitors "
            f"across {len(ip_hashes)} ip_hashes (last {days} days, > {threshold} visitors per IP-day)."
        )
        if events_by_ip:
            self.stdout.write("Top ip_hash prefixes by events:")
            for ip_hash, count in events_by_ip.most_common(10):
                self.stdout.write(f"  {ip_hash[:8]}  {count}")
        for day, count, detail in spikes:
            self.stdout.write(
                f"Spike {day}: {count} single-hit visitors vs a median of {detail['baseline']:.0f}; "
                f"{detail['direct_share']:.0%} direct, top browser {detail['ua_family']:.0%}, "
                f"top country {detail['country']:.0%}, top IP {detail['ip_hash']:.0%}."
            )
        if not apply:
            self.stdout.write("Dry run only — re-run with --apply to write the flags.")

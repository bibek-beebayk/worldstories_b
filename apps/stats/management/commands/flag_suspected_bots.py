from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.stats.models import AnalyticsEvent

# The ingest UA gate only catches bots that announce themselves. This finds
# the ones that don't: a single IP producing many distinct visitor_ids in a
# day, none of which ever did anything but load a page.
#
# Caution: many real users can share one IP on carrier-grade NAT (mobile
# carriers, campus and office networks). That is why the rule requires ZERO
# engagement from every visitor behind the IP-day — a shared IP full of
# genuine readers always has reading/listening/completion/download events —
# and why this defaults to a dry run. Nothing is deleted; flagged rows are
# only excluded from the dashboard's audience metrics, and --unflag reverses it.


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


class Command(BaseCommand):
    help = (
        "Flag AnalyticsEvent rows from IP-days that look like undeclared bots "
        "(many visitors, zero engagement). Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--min-visitors-per-ip", type=int, default=15)
        parser.add_argument("--apply", action="store_true", help="Write the flags (default is a dry run).")
        parser.add_argument("--unflag", action="store_true", help="Clear flags in the window instead of setting them.")

    def handle(self, *args, **options):
        days = options["days"]
        threshold = options["min_visitors_per_ip"]
        if days < 1 or threshold < 1:
            raise CommandError("--days and --min-visitors-per-ip must be at least 1.")
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
        for ip_hash, day, visitor_ids in _candidate_groups(cutoff, threshold):
            rows = (
                AnalyticsEvent.objects.filter(
                    created_at__gte=cutoff,
                    user__isnull=True,
                    ip_hash=ip_hash,
                    visitor_id__in=visitor_ids,
                    is_suspected_bot=False,
                )
                .annotate(day=TruncDate("created_at"))
                .filter(day=day)
            )
            ids = list(rows.values_list("id", flat=True))
            ip_hashes.add(ip_hash)
            visitors.update(visitor_ids)
            events_by_ip[ip_hash] += len(ids)
            event_ids.extend(ids)

        if apply and event_ids:
            for start in range(0, len(event_ids), 5000):
                AnalyticsEvent.objects.filter(id__in=event_ids[start : start + 5000]).update(is_suspected_bot=True)

        verb = "Flagged" if apply else "Would flag"
        self.stdout.write(
            f"[{mode}] {verb} {len(event_ids)} events from {len(visitors)} visitors "
            f"across {len(ip_hashes)} ip_hashes (last {days} days, > {threshold} visitors per IP-day)."
        )
        if events_by_ip:
            self.stdout.write("Top ip_hash prefixes by events:")
            for ip_hash, count in events_by_ip.most_common(10):
                self.stdout.write(f"  {ip_hash[:8]}  {count}")
        if not apply:
            self.stdout.write("Dry run only — re-run with --apply to write the flags.")

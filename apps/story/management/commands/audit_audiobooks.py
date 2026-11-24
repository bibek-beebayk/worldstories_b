"""Read-only integrity audit for Audiobook, Audio, and AudioReadingProgress data.

Exits with code 1 if any hard violation (items 1, 3, 4, 5, 6, 7, 9) is > 0.
Item 2 (published audiobooks with zero tracks) is reported as a warning only.
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import Count, F, Q

from apps.stats.models import AudioReadingProgress
from apps.story.models import Audio, Audiobook, Story


def compute_expected_audiobook_totals(audiobook: Audiobook) -> tuple[int, float | None]:
    durations = list(
        audiobook.tracks.filter(audio_file__isnull=False)
        .exclude(audio_file="")
        .values_list("duration_seconds", flat=True)
    )
    track_count = len(durations)
    if not durations or any(d is None for d in durations):
        total_duration = None
    else:
        total_duration = float(sum(durations))
    return track_count, total_duration


def _totals_differ(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) > 1e-3


def find_audio_fk_models() -> list[str]:
    found: list[str] = []
    for app_label in ("stats", "story"):
        try:
            app_config = apps.get_app_config(app_label)
        except LookupError:
            continue
        for model in app_config.get_models():
            for field in model._meta.get_fields():
                if not getattr(field, "is_relation", False) or not getattr(field, "many_to_one", False):
                    continue
                related = getattr(field, "related_model", None)
                if related in (Audio, Audiobook, Story):
                    if app_label == "stats" or related in (Audio, Audiobook):
                        found.append(f"{app_label}.{model.__name__}.{field.name} -> {related.__name__}")
    return sorted(set(found))


class Command(BaseCommand):
    help = "Audit Audiobook, Audio, and AudioReadingProgress integrity (read-only)."

    def handle(self, *args, **options):
        # 1. Audio rows with audiobook IS NULL
        orphan_audios = Audio.objects.filter(audiobook__isnull=True).count()

        # 2. Published audiobooks with zero tracks (warning only)
        published_empty_audiobooks = (
            Audiobook.objects.published()
            .annotate(_tc=Count("tracks"))
            .filter(_tc=0)
            .count()
        )

        # 3. Audiobooks whose cached totals differ from real values
        stale_cache_count = 0
        for ab in Audiobook.objects.all().iterator(chunk_size=500):
            expected_count, expected_total = compute_expected_audiobook_totals(ab)
            if ab.track_count != expected_count or _totals_differ(
                ab.total_duration_seconds, expected_total
            ):
                stale_cache_count += 1

        # 4. Duplicate (audiobook, order) pairs
        dup_track_orders = (
            Audio.objects.filter(audiobook__isnull=False)
            .values("audiobook_id", "order")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
            .count()
        )

        # 5. AudioReadingProgress rows with audiobook IS NULL but resolvable
        resolvable_null_progress = (
            AudioReadingProgress.objects.filter(audiobook__isnull=True)
            .filter(
                Q(audio__audiobook__isnull=False)
                | Q(story__audiobooks__isnull=False)
            )
            .distinct()
            .count()
        )

        # 6. AudioReadingProgress rows whose track does not belong to row's audiobook
        mismatched_progress = (
            AudioReadingProgress.objects.filter(audiobook__isnull=False)
            .exclude(audio__audiobook_id=F("audiobook_id"))
            .count()
        )

        # 7. Slug collisions between Audiobook slugs and unrelated published Story slugs
        ab_slugs = dict(Audiobook.objects.values_list("slug", "story_id"))
        colliding_stories = (
            Story.objects.published()
            .filter(slug__in=ab_slugs.keys())
            .values_list("id", "slug")
        )
        slug_collisions = sum(
            1 for story_id, slug in colliding_stories if ab_slugs.get(slug) != story_id
        )

        # 8. Models in apps.stats / apps.story with FKs to Audio/Audiobook/Story
        fk_models = find_audio_fk_models()

        # 9. Duplicate (user, audiobook, audio) progress rows
        dup_progress_keys = (
            AudioReadingProgress.objects.filter(audiobook__isnull=False)
            .values("user_id", "audiobook_id", "audio_id")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
            .count()
        )

        self.stdout.write("=== Audiobook Audit Report ===")
        self.stdout.write(f"1. Audio rows with audiobook IS NULL: {orphan_audios}")
        self.stdout.write(
            f"2. Published audiobooks with zero tracks (warning only): {published_empty_audiobooks}"
        )
        self.stdout.write(
            f"3. Audiobooks with stale cached totals (track_count / total_duration_seconds): {stale_cache_count}"
        )
        self.stdout.write(f"4. Duplicate (audiobook, order) track pairs: {dup_track_orders}")
        self.stdout.write(
            f"5. AudioReadingProgress rows with audiobook IS NULL (resolvable): {resolvable_null_progress}"
        )
        self.stdout.write(
            f"6. AudioReadingProgress rows where track.audiobook != row.audiobook: {mismatched_progress}"
        )
        self.stdout.write(
            f"7. Slug collisions between Audiobook and unrelated published Story: {slug_collisions}"
        )
        self.stdout.write(
            f"8. Models with ForeignKey to Audio/Audiobook/Story ({len(fk_models)}):"
        )
        for entry in fk_models:
            self.stdout.write(f"   - {entry}")
        self.stdout.write(
            f"9. Duplicate (user, audiobook, audio) progress rows: {dup_progress_keys}"
        )

        violations = (
            orphan_audios
            + stale_cache_count
            + dup_track_orders
            + resolvable_null_progress
            + mismatched_progress
            + slug_collisions
            + dup_progress_keys
        )
        if violations > 0:
            self.stderr.write(self.style.ERROR(f"Audit FAILED with {violations} violation(s)."))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("Audit PASSED (0 violations)."))

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from apps.story.author_merge import merge_authors
from apps.story.models import Author, COUNTRY_CHOICES, published_story_q


CSV_FIELDS = {
    "sort_name": "sort_name",
    "birth_year": "birth_year",
    "death_year": "death_year",
    "years_label": "years_label",
    "country_code": "country",
    "country": "country_label",
    "era": "era",
    "known_for": "known_for",
    "is_traditional": "is_traditional",
    "is_house": "is_house",
}
BOOL_FIELDS = {"is_traditional", "is_house"}
INT_FIELDS = {"birth_year", "death_year"}


def parse_value(field, raw):
    value = (raw or "").strip()
    if not value:
        return None
    if field in BOOL_FIELDS:
        return value.lower() in {"1", "true", "yes"}
    if field in INT_FIELDS:
        return int(value)
    return value


class Command(BaseCommand):
    help = "Preview or apply the curated author-directory CSV backfill."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--apply", action="store_true", help="Write changes; default is a dry run.")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")
        apply_changes = options["apply"]
        valid_countries = {code for code, _ in COUNTRY_CHOICES}
        rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
        matched_ids = set()
        summary = {"matched": 0, "changed": 0, "merge": 0, "review": 0, "unmatched": 0}

        self.stdout.write("APPLY" if apply_changes else "DRY RUN — no database changes will be written")
        with transaction.atomic():
            for index, row in enumerate(rows, 1):
                current_name = (row.get("current_name") or "").strip()
                display_name = (row.get("display_name") or current_name).strip()
                action = (row.get("action") or "keep").strip().lower()
                expected_titles = int(row["current_titles"]) if (row.get("current_titles") or "").strip() else None
                candidates = list(
                    Author.objects.filter(name=current_name)
                    .annotate(_published=Count("stories", filter=published_story_q("stories"), distinct=True))
                    .order_by("id")
                )
                if not candidates and display_name != current_name:
                    candidates = list(
                        Author.objects.filter(name=display_name)
                        .annotate(_published=Count("stories", filter=published_story_q("stories"), distinct=True))
                        .order_by("id")
                    )
                author = None
                if len(candidates) == 1:
                    author = candidates[0]
                elif candidates and expected_titles is not None:
                    author = next((item for item in candidates if item._published == expected_titles), None)

                if author is None and action == "merge":
                    target_exists = Author.objects.filter(name=display_name).annotate(
                        _published=Count("stories", filter=published_story_q("stories"), distinct=True)
                    ).filter(_published__gte=1).exists()
                    if target_exists:
                        self.stdout.write(f"[{index}] {current_name} ({expected_titles} titles): merge already complete")
                        continue
                if author is None:
                    summary["unmatched"] += 1
                    self.stdout.write(self.style.WARNING(f"[{index}] UNMATCHED: {current_name} ({expected_titles} titles)"))
                    continue

                matched_ids.add(author.pk)
                summary["matched"] += 1
                if action == "review":
                    summary["review"] += 1
                    self.stdout.write(f"[{index}] {current_name}: needs manual review — {row.get('notes', '').strip()}")
                    continue

                if action == "merge":
                    target = (
                        Author.objects.filter(name=display_name)
                        .exclude(pk=author.pk)
                        .annotate(_published=Count("stories", filter=published_story_q("stories"), distinct=True))
                        .order_by("-_published", "id")
                        .first()
                    )
                    if target is None:
                        summary["unmatched"] += 1
                        self.stdout.write(self.style.WARNING(f"[{index}] MERGE TARGET MISSING: {display_name}"))
                        continue
                    relations = []
                    for relation in author._meta.related_objects:
                        count = relation.related_model._default_manager.filter(**{relation.field.name: author}).count()
                        if count:
                            relations.append(f"{relation.related_model._meta.label}.{relation.field.name}={count}")
                    summary["merge"] += 1
                    self.stdout.write(f"[{index}] MERGE #{author.pk} {author.name} -> #{target.pk} {target.name}; move: {', '.join(relations) or 'nothing'}")
                    if apply_changes:
                        moved = merge_authors(author, target)
                        self.stdout.write("    moved exactly: " + (", ".join(f"{label}={count}" for label, count in moved) or "nothing"))
                    continue

                changes = {}
                for csv_name, model_name in CSV_FIELDS.items():
                    value = parse_value(model_name, row.get(csv_name))
                    if value is None:
                        continue
                    if model_name == "country" and value not in valid_countries:
                        self.stdout.write(self.style.WARNING(f"    unsupported country code {value}; leaving country blank"))
                        continue
                    if getattr(author, model_name) != value:
                        changes[model_name] = value
                if action in {"rename", "house"} and author.name != display_name:
                    changes["name"] = display_name
                if action == "house" and not author.is_house:
                    changes["is_house"] = True
                change_text = ", ".join(f"{key}: {getattr(author, key)!r} -> {value!r}" for key, value in changes.items())
                self.stdout.write(f"[{index}] MATCH #{author.pk} {current_name} [{action}]: {change_text or 'no changes'}")
                if changes:
                    summary["changed"] += 1
                    if apply_changes:
                        for field, value in changes.items():
                            setattr(author, field, value)
                        author.save(update_fields=list(changes))

            missing = Author.objects.exclude(pk__in=matched_ids).order_by("name", "id")
            self.stdout.write("DB AUTHORS MISSING FROM CSV:")
            if missing.exists():
                for author in missing:
                    self.stdout.write(f"  #{author.pk} {author.name}")
            else:
                self.stdout.write("  none")
            self.stdout.write(
                "SUMMARY: " + ", ".join(f"{key}={value}" for key, value in summary.items()) + f", csv_rows={len(rows)}"
            )
            if not apply_changes:
                transaction.set_rollback(True)

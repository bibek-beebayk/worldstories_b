"""Bulk-edits tags/themes/genres/categories on ALREADY-PUBLISHED Story rows
via an admin-uploaded CSV/Excel file, matched by title (+ author_name to
disambiguate). Distinct from queue_import.py, which creates NEW StoryQueue
rows rather than editing existing Story rows — this reuses queue_records.py's
resolve_tags/resolve_themes/resolve_genres/resolve_categories directly so
both features stay on identical case-insensitive match/create rules, and
import_parsing.py's shared file parsing rather than queue_import.py's
StoryQueue-specific column aliases.

Category names are never auto-created here, under any circumstance — that's
a hard rule (see resolve_categories(create_missing=False) below), not a
default the caller/request can override. "Science Fiction" is a specifically
forbidden category value (the correct category for that content is "Classic
Literature") — flagged, never silently substituted, and never written even
if a client sends it anyway.
"""
from typing import Dict, List, Optional

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from .import_parsing import ImportFileError, _FILE_VALIDATORS, _rows_from_csv, _rows_from_xlsx
from .models import Category, Genre, Story, Tag, Theme
from .queue_import import MAX_IMPORT_ROWS, _split_multi
from .queue_records import resolve_categories, resolve_genres, resolve_tags, resolve_themes

# Only maps "author" -> "author_name"; every other header is used as-is
# (title, tags, themes, genres, categories all already match their
# canonical names once lowercased/underscored).
_COLUMN_ALIASES = {"author": "author_name"}

FORBIDDEN_CATEGORY_NAME = "science fiction"


def _parse_taxonomy_file(uploaded_file) -> List[Dict[str, str]]:
    """Same validate-then-parse-then-cap shape as queue_import.parse_uploaded_file,
    just with this feature's own column-alias map — see import_parsing.py's
    module docstring for why the two features don't share this function."""
    try:
        for validator in _FILE_VALIDATORS:
            validator(uploaded_file)
    except DjangoValidationError as exc:
        raise ImportFileError("; ".join(exc.messages)) from exc

    file_bytes = uploaded_file.read()
    name = (uploaded_file.name or "").lower()
    try:
        rows = (
            _rows_from_xlsx(file_bytes, _COLUMN_ALIASES)
            if name.endswith(".xlsx")
            else _rows_from_csv(file_bytes, _COLUMN_ALIASES)
        )
    except Exception as exc:
        raise ImportFileError(f"Could not read this file: {exc}") from exc

    if not rows:
        raise ImportFileError("No data rows found in the file.")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ImportFileError(
            f"Too many rows ({len(rows)}) — split the file into batches of {MAX_IMPORT_ROWS} or fewer."
        )
    return rows


def resolve_story_match(title: str, author_name: str = "") -> dict:
    """Zero matches -> not_found. One match -> matched. Multiple matches ->
    narrow by author.name__iexact; exactly one survivor -> matched,
    otherwise ambiguous with every title-matched candidate (not just the
    narrowed set — the admin needs to see all of them to pick the right
    author_name)."""
    title = (title or "").strip()
    candidates_qs = Story.objects.published().select_related("author").filter(title__iexact=title)
    candidates = list(candidates_qs)
    if not candidates:
        return {"status": "not_found", "story": None, "candidates": []}
    if len(candidates) == 1:
        return {"status": "matched", "story": candidates[0], "candidates": []}

    author_name = (author_name or "").strip()
    if author_name:
        narrowed = list(candidates_qs.filter(author__name__iexact=author_name))
        if len(narrowed) == 1:
            return {"status": "matched", "story": narrowed[0], "candidates": []}

    return {"status": "ambiguous", "story": None, "candidates": candidates}


def _diff_field(current_names: List[str], proposed_names: List[str]) -> dict:
    """Case-insensitive diff shared by all four taxonomy fields. Returns
    display names as they appear on each respective side (current's own
    casing for removed, the file's casing for added)."""
    current_by_key = {name.strip().lower(): name.strip() for name in current_names if name.strip()}
    proposed_by_key = {name.strip().lower(): name.strip() for name in proposed_names if name.strip()}
    added = sorted(
        (proposed_by_key[key] for key in proposed_by_key if key not in current_by_key),
        key=str.lower,
    )
    removed = sorted(
        (current_by_key[key] for key in current_by_key if key not in proposed_by_key),
        key=str.lower,
    )
    unchanged_count = len(set(current_by_key) & set(proposed_by_key))
    return {"added": added, "removed": removed, "unchanged_count": unchanged_count}


def _empty_diff() -> dict:
    return {"added": [], "removed": [], "unchanged_count": 0}


def _new_names(model, names: List[str]) -> List[str]:
    """Raw names (deduped, original casing preserved, order kept) with no
    case-insensitive match in `model` — used for new_tags_to_create /
    new_themes_to_create / new_genres_to_create / new_categories_not_created,
    which all need this same "would this be a brand-new row" check."""
    existing_lower = {name.lower() for name in model.objects.values_list("name", flat=True)}
    seen = set()
    new_names = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if key not in existing_lower:
            new_names.append(name)
    return new_names


def _proposed_names(row: Dict[str, str], key: str) -> Optional[List[str]]:
    """None (leave this field unchanged on this row) for a blank/missing
    cell; a list (possibly empty, meaning "clear this field entirely") for
    any non-blank cell — see the module-level note on this distinction."""
    raw = row.get(key)
    if raw is None or not raw.strip():
        return None
    return _split_multi(raw)


def _current_names(story: Optional[Story], field: str) -> List[str]:
    if story is None:
        return []
    return sorted((item.name for item in getattr(story, field).all()), key=str.lower)


def _candidate_dict(story: Story) -> dict:
    return {
        "id": story.id,
        "slug": story.slug,
        "title": story.title,
        "author_name": story.author.name if story.author_id else "",
    }


def build_taxonomy_preview(uploaded_file) -> dict:
    """Parses the file, resolves each row's story match, diffs each of the
    four taxonomy fields against the matched story's current state, and
    flags the category-specific business rules. Writes nothing to the DB —
    tags/themes/genres are looked up only (create_missing=False) here, same
    as categories (which stay create_missing=False even at confirm)."""
    raw_rows = _parse_taxonomy_file(uploaded_file)  # may raise ImportFileError

    rows: List[dict] = []
    errors: List[str] = []
    matched_count = 0
    ambiguous_count = 0
    not_found_count = 0

    for row_number, row in enumerate(raw_rows, start=2):  # row 1 is the header
        title = (row.get("title") or "").strip()
        if not title:
            errors.append(f"Row {row_number}: missing required 'title' column.")
            continue
        author_name = (row.get("author_name") or "").strip()

        match = resolve_story_match(title, author_name)
        status = match["status"]
        story = match["story"]
        if status == "matched":
            matched_count += 1
        elif status == "ambiguous":
            ambiguous_count += 1
        else:
            not_found_count += 1

        proposed_tags = _proposed_names(row, "tags")
        proposed_themes = _proposed_names(row, "themes")
        proposed_genres = _proposed_names(row, "genres")
        proposed_categories = _proposed_names(row, "categories")

        row_out = {
            "title": title,
            "author_name": author_name,
            "match_status": status,
            "story_id": story.id if story else None,
            "story_slug": story.slug if story else None,
            "ambiguous_candidates": [_candidate_dict(s) for s in match["candidates"]],
        }

        for field, proposed in (
            ("tags", proposed_tags),
            ("themes", proposed_themes),
            ("genres", proposed_genres),
        ):
            current = _current_names(story, field)
            row_out[f"current_{field}"] = current
            row_out[f"proposed_{field}"] = proposed
            if proposed is None:
                diff = _empty_diff()
            else:
                diff = _diff_field(current, proposed)
            row_out[f"{field}_added"] = diff["added"]
            row_out[f"{field}_removed"] = diff["removed"]
            model = {"tags": Tag, "themes": Theme, "genres": Genre}[field]
            row_out[f"new_{field}_to_create"] = _new_names(model, proposed) if proposed is not None else []

        current_categories = _current_names(story, "categories")
        row_out["current_categories"] = current_categories
        row_out["proposed_categories"] = proposed_categories
        category_count_warning = None
        category_forbidden_value = None
        if proposed_categories is None:
            diff = _empty_diff()
            new_categories_not_created = []
        else:
            diff = _diff_field(current_categories, proposed_categories)
            new_categories_not_created = _new_names(Category, proposed_categories)
            forbidden = next(
                (name for name in proposed_categories if name.strip().lower() == FORBIDDEN_CATEGORY_NAME),
                None,
            )
            if forbidden:
                category_forbidden_value = forbidden.strip()
            # The final count is what confirm would actually leave the story
            # with — matched existing categories only, since new ones are
            # never created and unresolved names would just be dropped.
            resolved_count = len(resolve_categories(proposed_categories, create_missing=False))
            if resolved_count == 0 or resolved_count >= 3:
                category_count_warning = f"Resulting category count would be {resolved_count} (expected 1–2)."
        row_out["categories_added"] = diff["added"]
        row_out["categories_removed"] = diff["removed"]
        row_out["new_categories_not_created"] = new_categories_not_created
        row_out["category_count_warning"] = category_count_warning
        row_out["category_forbidden_value"] = category_forbidden_value

        rows.append(row_out)

    return {
        "rows": rows,
        "matched_count": matched_count,
        "ambiguous_count": ambiguous_count,
        "not_found_count": not_found_count,
        "errors": errors,
        "total_rows": len(raw_rows),
    }


def confirm_taxonomy_update(records: List[dict]) -> dict:
    """Re-resolves each record's story match defensively (the DB may have
    changed since preview) and applies only the fields that had a non-null
    proposed value. Categories stay create_missing=False here too — never
    auto-created, even at confirm — and any row still carrying a
    category_forbidden_value is skipped entirely rather than partially
    applied."""
    updated_count = 0
    skipped_count = 0
    errors: List[str] = []

    with transaction.atomic():
        for entry in records:
            if not isinstance(entry, dict):
                skipped_count += 1
                continue

            title = (entry.get("title") or "").strip()
            author_name = (entry.get("author_name") or "").strip()

            forbidden = entry.get("category_forbidden_value")
            if forbidden:
                skipped_count += 1
                errors.append(
                    f'"{title}": proposed categories include "{forbidden}", which is never allowed — skipped.'
                )
                continue

            match = resolve_story_match(title, author_name)
            if match["status"] != "matched":
                skipped_count += 1
                errors.append(f'"{title}": no longer matches a single published story — skipped.')
                continue
            story = match["story"]

            proposed_tags = entry.get("proposed_tags")
            if proposed_tags is not None:
                story.tags.set(resolve_tags(proposed_tags, create_missing=True))

            proposed_themes = entry.get("proposed_themes")
            if proposed_themes is not None:
                story.themes.set(resolve_themes(proposed_themes, create_missing=True))

            proposed_genres = entry.get("proposed_genres")
            if proposed_genres is not None:
                story.genres.set(resolve_genres(proposed_genres, create_missing=True))

            proposed_categories = entry.get("proposed_categories")
            if proposed_categories is not None:
                story.categories.set(resolve_categories(proposed_categories, create_missing=False))

            updated_count += 1

    return {"updated_count": updated_count, "skipped_count": skipped_count, "errors": errors}

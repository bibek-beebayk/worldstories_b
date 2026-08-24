"""Parses an admin-uploaded CSV/Excel file into candidate StoryQueue rows,
builds a review-before-write preview, and (once the admin confirms) creates
the reviewed rows. Reuses the exact same resolve/sanitize/dedupe rules as
the AI "Fetch Book Data" path — see queue_records.py.
"""
import csv
import io
import re
from typing import Dict, List, Optional, Tuple

import openpyxl
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import FileExtensionValidator
from django.db import transaction

from core.libs.validators import FileSizeValidator

from .book_fetch import _BookRecord
from .models import StoryQueue, format_original_published_date
from .queue_records import (
    dedupe_records,
    existing_queue_normalized_pairs,
    existing_story_normalized_pairs,
    resolve_categories,
    resolve_country,
    resolve_genres,
    resolve_language,
    resolve_story_type,
    sanitize_published_date,
    sanitize_url,
)

MAX_IMPORT_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_IMPORT_ROWS = 500

_FILE_VALIDATORS = [
    FileExtensionValidator(allowed_extensions=["csv", "xlsx"]),
    FileSizeValidator(MAX_IMPORT_FILE_SIZE),
]

# Maps common alternate header spellings onto the canonical StoryQueue-
# matching column names admins are asked to use.
_COLUMN_ALIASES = {
    "author": "author_name",
    "year": "original_published_year",
    "month": "original_published_month",
    "day": "original_published_day",
    "type": "story_type",
    "cover": "cover_image_link",
    "cover_image": "cover_image_link",
    "epub": "epub_link",
    "pdf": "pdf_link",
}


class ImportFileError(Exception):
    """Can't even start parsing — bad extension/size, unreadable file, no
    data rows, or too many rows. apps.story.api's import_preview action
    turns this into a 400 response."""


def _normalize_header(raw: str) -> str:
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return _COLUMN_ALIASES.get(key, key)


def _rows_from_csv(file_bytes: bytes) -> List[Dict[str, str]]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    headers = [_normalize_header(h) for h in rows[0]]
    return [dict(zip(headers, row)) for row in rows[1:] if any(cell.strip() for cell in row)]


def _rows_from_xlsx(file_bytes: bytes) -> List[Dict[str, str]]:
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return []
    headers = [_normalize_header(str(cell or "")) for cell in header_row]

    rows = []
    for row in rows_iter:
        if row is None or all(cell is None for cell in row):
            continue
        values = ["" if cell is None else str(cell).strip() for cell in row]
        rows.append(dict(zip(headers, values)))
    return rows


def parse_uploaded_file(uploaded_file) -> List[Dict[str, str]]:
    """Validates and parses an uploaded CSV/XLSX into raw row dicts (headers
    normalized, values as strings). Raises ImportFileError for anything that
    prevents parsing at all."""
    try:
        for validator in _FILE_VALIDATORS:
            validator(uploaded_file)
    except DjangoValidationError as exc:
        raise ImportFileError("; ".join(exc.messages)) from exc

    file_bytes = uploaded_file.read()
    name = (uploaded_file.name or "").lower()
    try:
        rows = _rows_from_xlsx(file_bytes) if name.endswith(".xlsx") else _rows_from_csv(file_bytes)
    except Exception as exc:
        raise ImportFileError(f"Could not read this file: {exc}") from exc

    if not rows:
        raise ImportFileError("No data rows found in the file.")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ImportFileError(
            f"Too many rows ({len(rows)}) — split the file into batches of {MAX_IMPORT_ROWS} or fewer."
        )
    return rows


def _split_multi(raw: str) -> List[str]:
    # Genres/categories: accept either ";" or "," as the in-cell separator —
    # by the time we have one cell's text, the file's own column-boundary
    # commas have already been resolved by the CSV/XLSX parser, so a comma
    # here is unambiguous.
    return [part.strip() for part in re.split(r"[;,]", raw) if part.strip()]


def _int_or_none(row: Dict[str, str], key: str) -> Optional[int]:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _row_to_record(row: Dict[str, str], row_number: int) -> Tuple[Optional[_BookRecord], Optional[str]]:
    title = (row.get("title") or "").strip()
    if not title:
        return None, f"Row {row_number}: missing required 'title' column."
    try:
        record = _BookRecord(
            title=title,
            author_name=(row.get("author_name") or "").strip(),
            about=(row.get("about") or "").strip(),
            story_type=(row.get("story_type") or "").strip(),
            country=(row.get("country") or "").strip(),
            language=(row.get("language") or "").strip(),
            genres=_split_multi(row.get("genres") or ""),
            categories=_split_multi(row.get("categories") or ""),
            original_published_year=_int_or_none(row, "original_published_year"),
            original_published_month=_int_or_none(row, "original_published_month"),
            original_published_day=_int_or_none(row, "original_published_day"),
            epub_link=(row.get("epub_link") or "").strip(),
            pdf_link=(row.get("pdf_link") or "").strip(),
            cover_image_link=(row.get("cover_image_link") or "").strip(),
        )
    except Exception as exc:
        return None, f"Row {row_number}: {exc}"
    return record, None


def _record_to_preview_dict(record: _BookRecord) -> dict:
    year, month, day = sanitize_published_date(
        record.original_published_year, record.original_published_month, record.original_published_day
    )
    return {
        "title": record.title.strip(),
        "author_name": record.author_name.strip(),
        "about": record.about.strip(),
        "story_type": resolve_story_type(record.story_type),
        "country": resolve_country(record.country),
        "language": resolve_language(record.language),
        "genres": [g.strip() for g in record.genres if g.strip()],
        "categories": [c.strip() for c in record.categories if c.strip()],
        "original_published_year": year,
        "original_published_month": month,
        "original_published_day": day,
        "published_date_label": format_original_published_date(year, month, day),
        "epub_link": sanitize_url(record.epub_link),
        "pdf_link": sanitize_url(record.pdf_link),
        "cover_image_link": sanitize_url(record.cover_image_link),
    }


def build_preview(uploaded_file) -> dict:
    """Parses+validates the file and resolves/sanitizes every row exactly
    like run_book_fetch does, then dedupes against Story/StoryQueue plus
    in-file duplicates. Writes nothing to the DB — genres/categories are
    looked up only (create_missing=False), never created, until confirm."""
    raw_rows = parse_uploaded_file(uploaded_file)  # may raise ImportFileError

    errors: List[str] = []
    records: List[_BookRecord] = []
    for row_number, row in enumerate(raw_rows, start=2):  # row 1 is the header
        record, error = _row_to_record(row, row_number)
        if error:
            errors.append(error)
        else:
            records.append(record)

    survivors, duplicates = dedupe_records(
        records, existing_story_normalized_pairs(), existing_queue_normalized_pairs()
    )

    to_add = [_record_to_preview_dict(record) for record in survivors]
    duplicate_dicts = [{**_record_to_preview_dict(record), "reason": reason} for record, reason in duplicates]

    return {
        "to_add": to_add,
        "duplicates": duplicate_dicts,
        "errors": errors,
        "to_add_count": len(to_add),
        "duplicate_count": len(duplicate_dicts),
        "error_count": len(errors),
        "total_rows": len(raw_rows),
    }


def confirm_import(records_data: List[dict]) -> Tuple[int, int]:
    """Re-validates + re-dedupes (defensive against a race since preview —
    e.g. someone else added the same book in the meantime) and creates
    StoryQueue rows for the confirmed batch. Returns (created_count,
    skipped_count)."""
    records: List[_BookRecord] = []
    for data in records_data:
        if not isinstance(data, dict):
            continue
        try:
            records.append(_BookRecord(**data))
        except Exception:
            continue  # malformed entry — client only ever sends what preview returned

    survivors, duplicates = dedupe_records(
        records, existing_story_normalized_pairs(), existing_queue_normalized_pairs()
    )

    created_count = 0
    with transaction.atomic():
        for record in survivors:
            year, month, day = sanitize_published_date(
                record.original_published_year, record.original_published_month, record.original_published_day
            )
            queue_item = StoryQueue.objects.create(
                title=record.title.strip(),
                author_name=record.author_name.strip(),
                about=record.about.strip() or None,
                story_type=resolve_story_type(record.story_type),
                country=resolve_country(record.country),
                language=resolve_language(record.language),
                original_published_year=year,
                original_published_month=month,
                original_published_day=day,
                epub_link=sanitize_url(record.epub_link),
                pdf_link=sanitize_url(record.pdf_link),
                cover_image_link=sanitize_url(record.cover_image_link),
            )
            queue_item.genres.set(resolve_genres(record.genres))
            queue_item.categories.set(resolve_categories(record.categories))
            created_count += 1

    return created_count, len(duplicates)

"""Shared CSV/XLSX upload parsing — extracted from queue_import.py so the
Story Queue import and the taxonomy_bulk_update.py bulk-taxonomy-edit
feature can both parse an uploaded file into normalized row dicts without
duplicating the csv/openpyxl handling. Each caller supplies its own header-
alias map (queue_import.py's story-queue column names differ from
taxonomy_bulk_update.py's tags/themes/genres/categories columns), so nothing
here hardcodes a specific column schema.
"""
import csv
import io
from typing import Dict, List

import openpyxl
from django.core.validators import FileExtensionValidator

from core.libs.validators import FileSizeValidator

MAX_IMPORT_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

_FILE_VALIDATORS = [
    FileExtensionValidator(allowed_extensions=["csv", "xlsx"]),
    FileSizeValidator(MAX_IMPORT_FILE_SIZE),
]


class ImportFileError(Exception):
    """Can't even start parsing — bad extension/size, unreadable file, no
    data rows, or too many rows. Callers turn this into a 400 response."""


def _normalize_header(raw: str, aliases: Dict[str, str]) -> str:
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return aliases.get(key, key)


def _rows_from_csv(file_bytes: bytes, aliases: Dict[str, str]) -> List[Dict[str, str]]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    headers = [_normalize_header(h, aliases) for h in rows[0]]
    return [dict(zip(headers, row)) for row in rows[1:] if any(cell.strip() for cell in row)]


def _rows_from_xlsx(file_bytes: bytes, aliases: Dict[str, str]) -> List[Dict[str, str]]:
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return []
    headers = [_normalize_header(str(cell or ""), aliases) for cell in header_row]

    rows = []
    for row in rows_iter:
        if row is None or all(cell is None for cell in row):
            continue
        values = ["" if cell is None else str(cell).strip() for cell in row]
        rows.append(dict(zip(headers, values)))
    return rows

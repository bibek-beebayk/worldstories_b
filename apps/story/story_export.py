"""Builds the CSV for the admin Story Report page's "Export" action. Uses
the exact same column schema queue_import.py expects on upload, so an
exported file can be edited and re-imported (into this or another
environment's Story Queue) unchanged.
"""
import csv
import io

from .models import COUNTRY_CHOICES, LANGUAGE_CHOICES

_COUNTRY_CODE_TO_NAME = dict(COUNTRY_CHOICES)
_LANGUAGE_CODE_TO_NAME = dict(LANGUAGE_CHOICES)

EXPORT_COLUMNS = [
    "title",
    "author_name",
    "about",
    "story_type",
    "country",
    "language",
    "genres",
    "categories",
    "original_published_year",
    "original_published_month",
    "original_published_day",
    "epub_link",
    "pdf_link",
    "cover_image_link",
]


def _file_url(file_field, request) -> str:
    if not file_field:
        return ""
    return request.build_absolute_uri(file_field.url) if request else file_field.url


def _story_row(story, request) -> list:
    return [
        story.title,
        story.author.name if story.author_id else "",
        story.about or "",
        story.story_type or "",
        _COUNTRY_CODE_TO_NAME.get(story.country, ""),
        _LANGUAGE_CODE_TO_NAME.get(story.language, ""),
        ", ".join(genre.name for genre in story.genres.all()),
        ", ".join(category.name for category in story.categories.all()),
        story.original_published_year or "",
        story.original_published_month or "",
        story.original_published_day or "",
        _file_url(story.epub_file, request),
        _file_url(story.pdf_file, request),
        _file_url(story.cover_image_file, request) or (story.cover_image or ""),
    ]


def build_story_export_csv(queryset, request=None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for story in queryset.prefetch_related("genres", "categories"):
        writer.writerow(_story_row(story, request))
    return buf.getvalue()

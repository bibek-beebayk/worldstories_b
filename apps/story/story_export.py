"""Builds the CSV for the admin Story Report page's "Export" action. Uses
the exact same column schema queue_import.py expects on upload, so an
exported file can be edited and re-imported (into this or another
environment's Story Queue) unchanged.

Can also append every not-yet-added StoryQueue row (is_added=False) after
the Story rows, using the same columns — otherwise a "back up everything and
re-import elsewhere" export would silently drop whatever's still sitting in
the queue. include_stories/include_queue (both default True) let the caller
pick either source, both, or (degenerately) neither.
"""
import csv
import io

from .models import COUNTRY_CHOICES, LANGUAGE_CHOICES, StoryQueue

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
    "tags",
    "themes",
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
        story.story_type.name,
        _COUNTRY_CODE_TO_NAME.get(story.country, ""),
        _LANGUAGE_CODE_TO_NAME.get(story.language, ""),
        ", ".join(genre.name for genre in story.genres.all()),
        ", ".join(category.name for category in story.categories.all()),
        ", ".join(tag.name for tag in story.tags.all()),
        ", ".join(theme.name for theme in story.themes.all()),
        story.original_published_year or "",
        story.original_published_month or "",
        story.original_published_day or "",
        _file_url(story.epub_file, request),
        _file_url(story.pdf_file, request),
        _file_url(story.cover_image_file, request) or (story.cover_image or ""),
    ]


def _queue_row(queue_item) -> list:
    # StoryQueue's link/cover fields are already plain URLs (unlike Story's
    # file fields), so no _file_url resolution needed here.
    return [
        queue_item.title,
        queue_item.author_name,
        queue_item.about or "",
        queue_item.story_type.name if queue_item.story_type_id else "",
        _COUNTRY_CODE_TO_NAME.get(queue_item.country, ""),
        _LANGUAGE_CODE_TO_NAME.get(queue_item.language, ""),
        ", ".join(genre.name for genre in queue_item.genres.all()),
        ", ".join(category.name for category in queue_item.categories.all()),
        ", ".join(tag.name for tag in queue_item.tags.all()),
        ", ".join(theme.name for theme in queue_item.themes.all()),
        queue_item.original_published_year or "",
        queue_item.original_published_month or "",
        queue_item.original_published_day or "",
        queue_item.epub_link,
        queue_item.pdf_link,
        queue_item.cover_image_link,
    ]


def build_story_export_csv(queryset, request=None, include_stories=True, include_queue=True) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    if include_stories:
        for story in queryset.select_related("story_type").prefetch_related(
            "genres", "categories", "tags", "themes"
        ):
            writer.writerow(_story_row(story, request))
    if include_queue:
        not_added = StoryQueue.objects.filter(is_added=False).select_related("story_type").prefetch_related(
            "genres", "categories", "tags", "themes"
        )
        for queue_item in not_added:
            writer.writerow(_queue_row(queue_item))
    return buf.getvalue()

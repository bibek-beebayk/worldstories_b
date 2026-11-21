from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from django.conf import settings
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import Truncator

from apps.story.models import Blog, Story
from apps.users.continue_reading import continue_reading_items

from .models import DigestIssue


def _absolute_url(value):
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return urljoin(f"{settings.NEWSLETTER_SITE_URL}/", value.lstrip("/"))


def _cover_url(story):
    if story.cover_image_file:
        return _absolute_url(story.cover_image_file.url)
    return _absolute_url(story.cover_image or "")


def _short_text(value, length):
    return Truncator(strip_tags(value or "").strip()).chars(length)


def _story_data(story):
    genre = next(iter(story.genres.all()), None)
    read_time = (
        story.cached_chapter_reading_minutes or story.cached_file_reading_minutes
    )
    labels = [story.story_type.name if story.story_type_id else "Story"]
    if genre:
        labels.append(genre.name)
    return {
        "title": story.title,
        "slug": story.slug,
        "cover_url": _cover_url(story),
        "description": _short_text(story.about or story.summary, 110),
        "label": " · ".join(labels),
        "read_time_minutes": read_time,
    }


def _as_datetime(since):
    if isinstance(since, datetime):
        return since
    if isinstance(since, date):
        return timezone.make_aware(datetime.combine(since, datetime.min.time()))
    raise TypeError("since must be a date or datetime")


def build_digest(since, subscription=None, story_slug=None):
    since = _as_datetime(since)
    stories = list(
        Story.objects.published()
        .filter(site_published_date__gte=timezone.localdate(since))
        .select_related("story_type", "author")
        .prefetch_related("genres")
        .order_by("-site_published_date", "-id")[:5]
    )
    posts = list(
        Blog.objects.published()
        .annotate(first_published_at=Coalesce("publish_at", "created_at"))
        .filter(first_published_at__gte=since)
        .order_by("-first_published_at", "-id")[:2]
    )

    recent_feature_ids = (
        DigestIssue.objects.exclude(featured_story_id=None)
        .order_by("-issue_date")
        .values_list("featured_story_id", flat=True)[:8]
    )
    spotlight_qs = (
        Story.objects.published()
        .select_related("story_type", "author")
        .prefetch_related("genres")
    )
    if story_slug:
        spotlight = spotlight_qs.filter(slug=story_slug).first()
    else:
        spotlight = (
            spotlight_qs.exclude(id__in=recent_feature_ids)
            .order_by("-rating", "-views", "-id")
            .first()
        )

    continue_reading = None
    if subscription and subscription.user_id:
        selected = continue_reading_items(
            subscription.user,
            since=timezone.now() - timedelta(days=30),
            published_only=True,
        )
        if selected:
            row = selected[0]
            progress = row["progress"]
            continue_reading = {
                "title": progress.story.title,
                "cover_url": _cover_url(progress.story),
                "chapter_number": row["chapter_number"],
                "total_chapters": row["total_chapters"],
                "percent": round(row["overall_progress"] * 100),
                "reader_path": f"/read/{progress.story.slug}/{progress.chapter.slug}",
            }

    return {
        "since": since,
        "new_stories": [_story_data(story) for story in stories],
        "new_posts": [
            {
                "title": post.title,
                "slug": post.slug,
                "reading_time_minutes": post.reading_time_minutes,
            }
            for post in posts
        ],
        "story_of_the_week": (
            {
                **_story_data(spotlight),
                "hook": _short_text(spotlight.summary or spotlight.about, 180),
            }
            if spotlight
            else None
        ),
        "story_of_the_week_id": spotlight.id if spotlight else None,
        "continue_reading": continue_reading,
    }

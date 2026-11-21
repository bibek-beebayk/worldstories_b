from django.db.models import Count

from apps.stats.models import ChapterReadingProgress, ReadingProgress, StoryCompletion
from apps.story.models import Chapter, published_story_q


def continue_reading_items(user, since=None, published_only=False):
    """Return the same in-progress chapter rows used by the library shelf.

    Callers add their own presentation data; selection and overall-progress
    maths live here so the digest cannot drift from the product shelf.
    """
    progress_qs = ReadingProgress.objects.filter(user=user).select_related(
        "story", "chapter"
    )
    if since is not None:
        progress_qs = progress_qs.filter(updated_at__gte=since)
    if published_only:
        progress_qs = progress_qs.filter(published_story_q("story"))
    progress_qs = progress_qs.exclude(
        story_id__in=StoryCompletion.objects.filter(user=user).values("story_id")
    ).order_by("-updated_at")

    progress_rows = list(progress_qs)
    story_ids = [item.story_id for item in progress_rows if item.story_id]
    chapter_progress_by_story = {}
    for item in ChapterReadingProgress.objects.filter(
        user=user, story_id__in=story_ids
    ).select_related("chapter"):
        chapter_progress_by_story.setdefault(item.story_id, {})[item.chapter.slug] = (
            max(0.0, min(1.0, item.progress))
        )
    chapter_counts = (
        dict(
            Chapter.objects.filter(story_id__in=story_ids)
            .values_list("story_id")
            .annotate(total=Count("id"))
        )
        if story_ids
        else {}
    )

    payload = []
    for item in progress_rows:
        total_chapters = chapter_counts.get(item.story_id, 0)
        values = chapter_progress_by_story.get(item.story_id, {})
        overall_progress = (
            sum(values.values()) / total_chapters if total_chapters else 0.0
        )
        if overall_progress <= 0 or overall_progress >= 1:
            continue
        payload.append(
            {
                "progress": item,
                "overall_progress": round(overall_progress, 4),
                "total_chapters": total_chapters,
                "chapter_number": item.chapter.order if item.chapter else None,
            }
        )
    return payload

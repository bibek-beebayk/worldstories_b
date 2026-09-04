"""Backfills StoryCompletion from the two mechanisms it replaces.

Before this, "completed" existed only as an append-only analytics event
deduplicated by localStorage, and as a query that averaged chapter progress.
Neither survives a device change, and the second could not see audio-, video-
or file-only stories at all. Readers must not lose the stories they have
already finished, so both sources are replayed here.

Additive and idempotent: it only inserts rows, never edits or deletes
progress, and re-running it is a no-op because of the (user, story)
uniqueness constraint. Reverse is a deliberate no-op — dropping rows on a
downgrade would silently discard completions earned after the upgrade.
"""

from django.db import migrations

COMPLETION_THRESHOLD = 0.995


def _all_items_finished(item_ids, finished_ids):
    return bool(item_ids) and item_ids <= finished_ids


def backfill(apps, schema_editor):
    Story = apps.get_model("story", "Story")
    Chapter = apps.get_model("story", "Chapter")
    Audio = apps.get_model("story", "Audio")
    Video = apps.get_model("story", "Video")
    ChapterReadingProgress = apps.get_model("stats", "ChapterReadingProgress")
    AudioReadingProgress = apps.get_model("stats", "AudioReadingProgress")
    VideoWatchProgress = apps.get_model("stats", "VideoWatchProgress")
    FileReadingProgress = apps.get_model("stats", "FileReadingProgress")
    AnalyticsEvent = apps.get_model("stats", "AnalyticsEvent")
    StoryCompletion = apps.get_model("stats", "StoryCompletion")

    # Group every item id and every finished item id by story, up front, so
    # this is a handful of queries rather than a few per (user, story) pair.
    def items_by_story(model):
        grouped = {}
        for story_id, item_id in model.objects.values_list("story_id", "id"):
            grouped.setdefault(story_id, set()).add(item_id)
        return grouped

    def finished_by_pair(model, item_field):
        grouped = {}
        rows = model.objects.filter(progress__gte=COMPLETION_THRESHOLD).values_list(
            "user_id", "story_id", item_field
        )
        for user_id, story_id, item_id in rows:
            grouped.setdefault((user_id, story_id), set()).add(item_id)
        return grouped

    chapters = items_by_story(Chapter)
    audios = items_by_story(Audio)
    videos = items_by_story(Video)

    finished_chapters = finished_by_pair(ChapterReadingProgress, "chapter_id")
    finished_audios = finished_by_pair(AudioReadingProgress, "audio_id")
    finished_videos = finished_by_pair(VideoWatchProgress, "video_id")

    # source is recorded as "backfill" throughout: for a historical row we
    # know the story was finished, but not reliably on which surface, and
    # inventing one would misreport it.
    completed = {}

    def note(user_id, story_id):
        completed.setdefault((user_id, story_id), True)

    for grouped, per_story in (
        (finished_chapters, chapters),
        (finished_audios, audios),
        (finished_videos, videos),
    ):
        for (user_id, story_id), finished_ids in grouped.items():
            if _all_items_finished(per_story.get(story_id, set()), finished_ids):
                note(user_id, story_id)

    file_stories = dict(Story.objects.values_list("id", "epub_file"))
    pdf_stories = dict(Story.objects.values_list("id", "pdf_file"))
    for user_id, story_id, file_format in FileReadingProgress.objects.filter(
        progress__gte=COMPLETION_THRESHOLD
    ).values_list("user_id", "story_id", "format"):
        has_file = file_stories.get(story_id) if file_format == "epub" else pdf_stories.get(story_id)
        if has_file:
            note(user_id, story_id)

    # The historical analytics events. Only the story-level ones: a per-chapter
    # or per-track completion says an item finished, not the whole story, and
    # the progress rows above already settle that case more reliably.
    for user_id, story_id in AnalyticsEvent.objects.filter(
        event_type="completion",
        user_id__isnull=False,
        story_id__isnull=False,
        metadata__content_type="story",
    ).values_list("user_id", "story_id"):
        note(user_id, story_id)

    existing = set(StoryCompletion.objects.values_list("user_id", "story_id"))
    StoryCompletion.objects.bulk_create(
        [
            StoryCompletion(user_id=user_id, story_id=story_id, source="backfill")
            for (user_id, story_id) in completed
            if (user_id, story_id) not in existing
        ],
        batch_size=500,
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("stats", "0012_storycompletion"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

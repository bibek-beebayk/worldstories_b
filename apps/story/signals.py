"""Keeps Story.cached_chapter_reading_minutes in step with chapter content.

Chapter-based reading time is cheap to compute for one story and far too
expensive to compute per row in a list response (see reading_time.py), so the
value is denormalized onto Story. Recomputing here rather than in the admin
serializer means every write path keeps it correct — the custom admin API, the
Django admin, the EPUB import job, data migrations, and the shell.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import reading_time
from .models import Chapter, Story


def recompute_chapter_reading_minutes(story_id):
    """Recompute and store one story's cached chapter reading estimate.

    Uses .update() rather than .save() so this can never recurse through
    another save signal, and never clobbers a concurrent write to an
    unrelated field on the same story.
    """
    story = Story.objects.filter(pk=story_id).first()
    if story is None:
        return None
    minutes = reading_time.chapters_reading_minutes(story)
    Story.objects.filter(pk=story_id).update(cached_chapter_reading_minutes=minutes)
    return minutes


@receiver(post_save, sender=Chapter)
def chapter_saved(sender, instance, **kwargs):
    recompute_chapter_reading_minutes(instance.story_id)


@receiver(post_delete, sender=Chapter)
def chapter_deleted(sender, instance, **kwargs):
    # A chapter deleted as part of its story's own cascade leaves no story to
    # update; the filter().first() in the helper handles that without raising.
    recompute_chapter_reading_minutes(instance.story_id)

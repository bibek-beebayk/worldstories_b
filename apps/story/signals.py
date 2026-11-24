"""Signals keeping Story/Audiobook denormalized fields in sync.

1. Story.cached_chapter_reading_minutes on Chapter save/delete.
2. Audiobook auto-attachment & cache refresh (total_duration_seconds, track_count)
   on Audio save/delete.
3. AudioReadingProgress.audiobook auto-fill on save.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.stats.models import AudioReadingProgress
from . import reading_time
from .models import Audio, Audiobook, Chapter, Story


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


def unique_audiobook_slug(base_slug: str, reserved: set[str] | None = None) -> str:
    """Return a unique Audiobook slug starting with `base_slug`, falling back
    to `<base>-audiobook`, `<base>-audiobook-2`, etc. on collision."""
    base = (base_slug or "audiobook")[:240]
    taken = reserved if reserved is not None else set()

    def is_taken(candidate: str) -> bool:
        return candidate in taken or Audiobook.objects.filter(slug=candidate).exists()

    if not is_taken(base):
        return base
    alt = f"{base}-audiobook"[:256]
    if not is_taken(alt):
        return alt
    suffix = 2
    while True:
        candidate = f"{base[:240]}-audiobook-{suffix}"[:256]
        if not is_taken(candidate):
            return candidate
        suffix += 1


@receiver(post_save, sender=Chapter)
def chapter_saved(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    recompute_chapter_reading_minutes(instance.story_id)


@receiver(post_delete, sender=Chapter)
def chapter_deleted(sender, instance, **kwargs):
    recompute_chapter_reading_minutes(instance.story_id)


@receiver(post_save, sender=Audio)
def audio_saved(sender, instance, **kwargs):
    if kwargs.get("raw") or instance.audiobook_id is None:
        return
    audiobook = Audiobook.objects.filter(pk=instance.audiobook_id).first()
    if audiobook is not None:
        audiobook.refresh_cache()


@receiver(post_delete, sender=Audio)
def audio_deleted(sender, instance, **kwargs):
    if instance.audiobook_id is None:
        return
    audiobook = Audiobook.objects.filter(pk=instance.audiobook_id).first()
    if audiobook is not None:
        audiobook.refresh_cache()

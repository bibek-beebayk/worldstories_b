from django.apps import AppConfig


class StoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.story"

    def ready(self):
        # Registers the Chapter signals that maintain
        # Story.cached_chapter_reading_minutes.
        from . import signals  # noqa: F401

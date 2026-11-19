def default_story_type_id():
    """Resolve the seeded Short Story row without importing the live model."""
    from django.apps import apps

    StoryType = apps.get_model("story", "StoryType")
    return StoryType.objects.filter(name="Short Story").values_list("id", flat=True).first()

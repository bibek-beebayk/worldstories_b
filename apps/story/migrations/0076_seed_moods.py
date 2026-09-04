"""Seeds the mood vocabulary (§8.3).

The ten moods the source document lists, no more. "What are you in the mood
for?" stops working the moment the answer list needs scrolling, so this is a
closed vocabulary rather than a free-form taxonomy like Tag.

Re-runnable and non-destructive, for the same reasons as the achievement seed:
rows are matched on slug and updated in place, and the reverse deactivates
rather than deletes so a mood removed from this list keeps whatever stories
have already been assigned to it.
"""

from django.db import migrations

MOODS = [
    ("funny", "Funny", "😄", "Light, playful, made to be laughed at.", 1),
    ("scary", "Scary", "😱", "Unsettling, eerie, meant to frighten.", 2),
    ("magical", "Magical", "✨", "Enchantment, wonder, the impossible made ordinary.", 3),
    ("comforting", "Comforting", "🫖", "Gentle and warm — a story to be soothed by.", 4),
    ("emotional", "Emotional", "🥹", "Moving, tender, likely to sit with you.", 5),
    ("adventurous", "Adventurous", "🗡️", "Journeys, danger and momentum.", 6),
    ("mysterious", "Mysterious", "🔍", "Secrets, questions and slow revelation.", 7),
    ("inspiring", "Inspiring", "🌟", "Courage and resolve — a story that lifts.", 8),
    ("thought-provoking", "Thought-provoking", "🤔", "Stays with you as a question.", 9),
    ("romantic", "Romantic", "💛", "Love in its many forms.", 10),
]


def seed(apps, schema_editor):
    Mood = apps.get_model("story", "Mood")
    for slug, name, icon, description, order in MOODS:
        Mood.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "icon": icon,
                "description": description,
                "order": order,
                "active": True,
            },
        )


def deactivate(apps, schema_editor):
    Mood = apps.get_model("story", "Mood")
    Mood.objects.filter(slug__in=[row[0] for row in MOODS]).update(active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0075_moods"),
    ]

    operations = [
        migrations.RunPython(seed, deactivate),
    ]

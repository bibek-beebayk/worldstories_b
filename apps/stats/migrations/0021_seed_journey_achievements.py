"""Adds the Journeys achievements (§9.4's "optionally unlock an achievement").

Same conventions as the Milestone 6 seed: matched on slug, updated in place,
re-runnable, and the reverse deactivates rather than deletes so readers keep
what they have already earned.
"""

from django.db import migrations

ACHIEVEMENTS = [
    ("first-journey", "First Journey", "Complete your first Story Journey.", "journey", "🧭", "journeys_completed", 1, "", 1),
    ("three-journeys", "Three Journeys", "Complete 3 Story Journeys.", "journey", "🗺️", "journeys_completed", 3, "", 2),
]


def seed(apps, schema_editor):
    Achievement = apps.get_model("stats", "Achievement")
    for (
        slug, name, description, category, icon, target_type, target_value, target_key, order
    ) in ACHIEVEMENTS:
        Achievement.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "description": description,
                "category": category,
                "icon": icon,
                "target_type": target_type,
                "target_value": target_value,
                "target_key": target_key,
                "order": order,
                "active": True,
                "hidden": False,
            },
        )


def deactivate(apps, schema_editor):
    Achievement = apps.get_model("stats", "Achievement")
    Achievement.objects.filter(slug__in=[row[0] for row in ACHIEVEMENTS]).update(active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("stats", "0020_journey_events"),
    ]

    operations = [
        migrations.RunPython(seed, deactivate),
    ]

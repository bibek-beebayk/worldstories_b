"""Seeds the initial achievement catalogue (§6.2).

Re-runnable: every row is matched on its slug and updated in place, so running
this twice changes nothing and editing a name or description here and
re-applying is a safe way to correct copy. It never deletes, so an achievement
retired from this list keeps the awards readers already earned — taking a badge
back is not something a migration should do quietly.

The reverse deactivates rather than deletes, for the same reason.
"""

from django.db import migrations

ACHIEVEMENTS = [
    # (slug, name, description, category, icon, target_type, target_value, target_key, order)
    ("first-story", "First Story", "Finish your first story.", "reading", "📖", "stories_completed", 1, "", 1),
    ("ten-stories", "Ten Stories", "Finish 10 stories.", "reading", "📚", "stories_completed", 10, "", 2),
    ("twenty-five-stories", "Twenty-Five Stories", "Finish 25 stories.", "reading", "📚", "stories_completed", 25, "", 3),
    ("fifty-stories", "Fifty Stories", "Finish 50 stories.", "reading", "🏅", "stories_completed", 50, "", 4),
    ("hundred-stories", "One Hundred Stories", "Finish 100 stories.", "reading", "🏆", "stories_completed", 100, "", 5),

    ("five-countries", "Five Countries", "Finish a story from 5 different countries.", "countries", "🗺️", "countries_explored", 5, "", 1),
    ("ten-countries", "Ten Countries", "Finish a story from 10 different countries.", "countries", "🧭", "countries_explored", 10, "", 2),
    ("twenty-countries", "World Traveller", "Finish a story from 20 different countries.", "countries", "🌍", "countries_explored", 20, "", 3),

    # target_key is a Genre slug. Seeded from the source document's examples;
    # a genre that does not exist simply never progresses, so this cannot break
    # a site whose taxonomy differs.
    ("ten-folklore", "Folklore Reader", "Finish 10 folklore stories.", "genre", "🪔", "genre_completed", 10, "folklore", 1),
    ("ten-classic", "Classic Reader", "Finish 10 classic stories.", "genre", "🏛️", "genre_completed", 10, "classic", 2),
    ("ten-adventure", "Adventure Reader", "Finish 10 adventure stories.", "genre", "🧗", "genre_completed", 10, "adventure", 3),

    ("seven-day-streak", "Seven-Day Streak", "Read on seven days in a row.", "streak", "🔥", "streak_days", 7, "", 1),
    ("thirty-day-streak", "Thirty-Day Streak", "Read on thirty days in a row.", "streak", "🔥", "streak_days", 30, "", 2),

    ("ten-quick-reads", "Ten Quick Reads", "Finish 10 Quick Read summaries.", "quick_read", "⚡", "quick_reads_completed", 10, "", 1),
    ("twenty-five-quick-reads", "Twenty-Five Quick Reads", "Finish 25 Quick Read summaries.", "quick_read", "⚡", "quick_reads_completed", 25, "", 2),
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
        ("stats", "0017_achievements"),
    ]

    operations = [
        migrations.RunPython(seed, deactivate),
    ]

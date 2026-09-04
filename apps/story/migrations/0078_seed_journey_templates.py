"""Seeds the §9.2 example journeys as **inactive templates**.

Inactive on purpose. A journey with no stories in it is an editor's work in
progress, not something a reader should meet — so these arrive as named,
described shells for an editor to populate and then activate. The public
endpoints skip any journey with no required items regardless, so an unfinished
one can never leak.

Re-runnable and non-destructive: matched on slug, updated in place, and the
reverse deactivates rather than deletes.
"""

from django.db import migrations

JOURNEYS = [
    ("japanese-folklore", "Japanese Folklore", "country",
     "Tales carried down through generations in Japan.", 1),
    ("ghost-stories-around-the-world", "Ghost Stories Around the World", "theme",
     "What different cultures are afraid of, and why.", 2),
    ("stories-from-nepal", "Stories from Nepal", "country",
     "Folk tales from the Himalaya and the valleys below.", 3),
    ("trickster-tales", "Trickster Tales", "theme",
     "The clever, the sly and the very nearly caught.", 4),
    ("african-folk-wisdom", "African Folk Wisdom", "theme",
     "Stories told to teach, across the continent.", 5),
    ("classic-fairy-tales", "Classic Fairy Tales", "curated",
     "The tales most of us met first.", 6),
]


def seed(apps, schema_editor):
    StoryJourney = apps.get_model("story", "StoryJourney")
    for slug, title, journey_type, description, order in JOURNEYS:
        StoryJourney.objects.update_or_create(
            slug=slug,
            defaults={
                "title": title,
                "type": journey_type,
                "description": description,
                "order": order,
                # Left inactive: an editor adds the stories, then turns it on.
                "active": False,
            },
        )


def deactivate(apps, schema_editor):
    StoryJourney = apps.get_model("story", "StoryJourney")
    StoryJourney.objects.filter(slug__in=[row[0] for row in JOURNEYS]).update(active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0077_story_journeys"),
    ]

    operations = [
        migrations.RunPython(seed, deactivate),
    ]

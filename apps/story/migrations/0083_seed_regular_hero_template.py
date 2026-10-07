from django.db import migrations

# The homepage hero's content as it was hardcoded before templates existed,
# so switching the site over to templates changes nothing visible.
REGULAR = {
    "name": "Regular",
    "is_default": True,
    "title_prefix": "World",
    "title_highlight": "Stories",
    "description": (
        "The home for stories from around the world. Read novels, poetry, and short fiction for free, "
        "and discover audiobooks and read-along narrations from authors across every genre and country."
    ),
    "info_line_1_icon": "BookOpenText",
    "info_line_1_text": "Full novels, quick reads & poetry",
    "info_line_2_icon": "Headphones",
    "info_line_2_text": "Audiobooks & read-along narration",
}


def seed_regular(apps, schema_editor):
    HeroTemplate = apps.get_model("story", "HeroTemplate")
    if not HeroTemplate.objects.exists():
        HeroTemplate.objects.create(**REGULAR)


class Migration(migrations.Migration):
    dependencies = [("story", "0082_herotemplate")]

    operations = [migrations.RunPython(seed_regular, migrations.RunPython.noop)]

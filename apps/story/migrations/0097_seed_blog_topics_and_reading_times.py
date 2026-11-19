from django.db import migrations
from django.utils.html import strip_tags

# The starting topic vocabulary for the blog list's chips. Editable in the admin afterwards.
TOPICS = [
    ("Reading guides", "reading-guides"),
    ("Genre deep dives", "genre-deep-dives"),
    ("Book explainers", "book-explainers"),
    ("Author spotlights", "author-spotlights"),
    ("Folklore & myth", "folklore-myth"),
    ("Women writers", "women-writers"),
]

WORDS_PER_MINUTE = 230


def seed_topics(apps, schema_editor):
    BlogTopic = apps.get_model("story", "BlogTopic")
    for order, (name, slug) in enumerate(TOPICS):
        BlogTopic.objects.get_or_create(slug=slug, defaults={"name": name, "order": order})


def backfill_reading_times(apps, schema_editor):
    """Existing posts get their reading time now; new saves compute it themselves
    (Blog.save). Inlined, not imported, so the migration keeps working if that helper moves."""
    Blog = apps.get_model("story", "Blog")
    for blog in Blog.objects.all().only("id", "content"):
        words = len(strip_tags(blog.content or "").split())
        minutes = max(1, int(words / WORDS_PER_MINUTE + 0.5))
        Blog.objects.filter(pk=blog.pk).update(reading_time_minutes=minutes)


class Migration(migrations.Migration):
    dependencies = [("story", "0096_blog_topics_reading_time_featured")]

    operations = [
        migrations.RunPython(seed_topics, migrations.RunPython.noop),
        migrations.RunPython(backfill_reading_times, migrations.RunPython.noop),
    ]

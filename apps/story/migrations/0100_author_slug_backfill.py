from django.db import migrations
from django.utils.text import slugify


def backfill_slugs(apps, schema_editor):
    Author = apps.get_model("story", "Author")
    used = set()
    for author in Author.objects.order_by("id"):
        base = slugify(author.name) or "author"
        slug = base
        suffix = 2
        while slug in used or Author.objects.exclude(pk=author.pk).filter(slug=slug).exists():
            slug = f"{base}-{suffix}"
            suffix += 1
        author.slug = slug
        author.save(update_fields=["slug"])
        used.add(slug)


class Migration(migrations.Migration):
    dependencies = [("story", "0099_author_directory_fields")]

    operations = [
        migrations.RunPython(backfill_slugs, migrations.RunPython.noop),
    ]

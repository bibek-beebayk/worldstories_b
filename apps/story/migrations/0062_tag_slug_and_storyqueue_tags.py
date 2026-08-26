from django.db import migrations, models
from django.utils.text import slugify


def backfill_tag_slugs(apps, schema_editor):
    Tag = apps.get_model("story", "Tag")
    seen = set(Tag.objects.exclude(slug="").exclude(slug__isnull=True).values_list("slug", flat=True))
    for tag in Tag.objects.filter(models.Q(slug="") | models.Q(slug__isnull=True)).order_by("id"):
        base_slug = slugify(tag.name) or "tag"
        slug = base_slug
        index = 2
        while slug in seen:
            slug = f"{base_slug}-{index}"
            index += 1
        seen.add(slug)
        tag.slug = slug
        tag.save(update_fields=["slug"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0061_storyqueue_notes"),
    ]

    operations = [
        # Nullable + unique first — NULLs don't collide against a unique
        # constraint, so this step needs no default and can't conflict with
        # any existing rows, unlike adding a non-nullable field directly.
        migrations.AddField(
            model_name="tag",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, null=True, unique=True),
        ),
        migrations.RunPython(backfill_tag_slugs, noop_reverse),
        # Now that every row has a value, drop null=True to match the final
        # model definition (blank=True, unique=True, not nullable).
        migrations.AlterField(
            model_name="tag",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, unique=True),
        ),
        migrations.AddField(
            model_name="storyqueue",
            name="tags",
            field=models.ManyToManyField(blank=True, related_name="queue_items", to="story.tag"),
        ),
        migrations.AddField(
            model_name="tag",
            name="description",
            field=models.TextField(blank=True),
        ),
    ]

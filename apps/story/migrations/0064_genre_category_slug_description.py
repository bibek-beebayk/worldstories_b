from django.db import migrations, models
from django.utils.text import slugify


def _backfill_slugs(model, apps, schema_editor):
    Model = apps.get_model("story", model)
    seen = set(Model.objects.exclude(slug="").exclude(slug__isnull=True).values_list("slug", flat=True))
    for obj in Model.objects.filter(models.Q(slug="") | models.Q(slug__isnull=True)).order_by("id"):
        base_slug = slugify(obj.name) or model.lower()
        slug = base_slug
        index = 2
        while slug in seen:
            slug = f"{base_slug}-{index}"
            index += 1
        seen.add(slug)
        obj.slug = slug
        obj.save(update_fields=["slug"])


def backfill_genre_slugs(apps, schema_editor):
    _backfill_slugs("Genre", apps, schema_editor)


def backfill_category_slugs(apps, schema_editor):
    _backfill_slugs("Category", apps, schema_editor)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0063_theme_and_story_tags_related_name_fix"),
    ]

    operations = [
        # Nullable + unique first — same reasoning as Tag.slug in
        # 0062_tag_slug_and_storyqueue_tags.py: NULLs don't collide against
        # a unique constraint, so no default is needed and no existing row
        # can conflict, unlike adding a non-nullable unique field directly.
        migrations.AddField(
            model_name="genre",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="category",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, null=True, unique=True),
        ),
        migrations.RunPython(backfill_genre_slugs, noop_reverse),
        migrations.RunPython(backfill_category_slugs, noop_reverse),
        # Now that every row has a value, drop null=True to match the final
        # model definition (blank=True, unique=True, not nullable).
        migrations.AlterField(
            model_name="genre",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, unique=True),
        ),
        migrations.AlterField(
            model_name="category",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, unique=True),
        ),
        migrations.AddField(
            model_name="genre",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="category",
            name="description",
            field=models.TextField(blank=True),
        ),
    ]

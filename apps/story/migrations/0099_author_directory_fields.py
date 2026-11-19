from django.db import migrations, models
from django.utils.text import slugify
from apps.story.models import COUNTRY_CHOICES


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
    dependencies = [("story", "0098_video_series_and_display_title")]

    operations = [
        migrations.AddField(model_name="author", name="slug", field=models.SlugField(blank=True, max_length=140, null=True)),
        migrations.AddField(model_name="author", name="sort_name", field=models.CharField(blank=True, default="", max_length=140)),
        migrations.AddField(model_name="author", name="birth_year", field=models.SmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="author", name="death_year", field=models.SmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="author", name="years_label", field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(model_name="author", name="country", field=models.CharField(blank=True, choices=COUNTRY_CHOICES, default="", max_length=2)),
        migrations.AddField(model_name="author", name="country_label", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="author", name="era", field=models.CharField(blank=True, choices=[("ancient", "Ancient"), ("pre_1800", "Before 1800"), ("1800s", "1800s"), ("1900s", "1900s"), ("contemporary", "Contemporary")], default="", max_length=16)),
        migrations.AddField(model_name="author", name="known_for", field=models.CharField(blank=True, default="", max_length=180)),
        migrations.AddField(model_name="author", name="is_traditional", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="author", name="is_house", field=models.BooleanField(default=False)),
        migrations.RunPython(backfill_slugs, migrations.RunPython.noop),
        migrations.AlterField(model_name="author", name="slug", field=models.SlugField(max_length=140, unique=True)),
    ]

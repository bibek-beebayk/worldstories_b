from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0064_genre_category_slug_description"),
    ]

    operations = [
        migrations.AlterField(
            model_name="genre",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="category",
            name="slug",
            field=models.SlugField(blank=True, max_length=60, null=True, unique=True),
        ),
    ]

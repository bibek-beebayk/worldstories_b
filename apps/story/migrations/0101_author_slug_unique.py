from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("story", "0100_author_slug_backfill")]

    operations = [
        migrations.AlterField(
            model_name="author",
            name="slug",
            field=models.SlugField(max_length=140, unique=True),
        ),
    ]

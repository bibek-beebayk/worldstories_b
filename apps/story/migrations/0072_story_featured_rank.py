from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0071_story_is_original"),
    ]

    operations = [
        migrations.AddField(
            model_name="story",
            name="featured_rank",
            field=models.PositiveSmallIntegerField(blank=True, null=True, unique=True),
        ),
    ]

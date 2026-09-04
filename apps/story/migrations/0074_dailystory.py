import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("story", "0073_story_cached_chapter_reading_minutes")]

    operations = [
        migrations.CreateModel(
            name="DailyStory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(db_index=True, unique=True)),
                ("featured_reason", models.CharField(blank=True, max_length=280)),
                ("active", models.BooleanField(db_index=True, default=True)),
                ("story", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="daily_features", to="story.story")),
            ],
            options={"ordering": ["-date"]},
        ),
    ]

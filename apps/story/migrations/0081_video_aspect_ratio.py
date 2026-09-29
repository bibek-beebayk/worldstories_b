from django.db import migrations, models


def mark_existing_shorts_as_portrait(apps, schema_editor):
    Video = apps.get_model("story", "Video")
    Video.objects.filter(youtube_url__icontains="/shorts/").update(
        aspect_ratio="9:16"
    )


class Migration(migrations.Migration):
    dependencies = [("story", "0080_story_show_in_nepali_site")]

    operations = [
        migrations.AddField(
            model_name="video",
            name="aspect_ratio",
            field=models.CharField(
                choices=[
                    ("16:9", "Landscape (16:9)"),
                    ("9:16", "Portrait (9:16)"),
                ],
                default="16:9",
                max_length=5,
            ),
        ),
        migrations.RunPython(
            mark_existing_shorts_as_portrait,
            migrations.RunPython.noop,
        ),
    ]

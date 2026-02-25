from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0015_submission_published_story"),
    ]

    operations = [
        migrations.AddField(
            model_name="story",
            name="cover_image_file",
            field=models.ImageField(blank=True, null=True, upload_to="story_covers/"),
        ),
        migrations.AddField(
            model_name="submission",
            name="cover_image_file",
            field=models.ImageField(blank=True, null=True, upload_to="submission_covers/"),
        ),
    ]

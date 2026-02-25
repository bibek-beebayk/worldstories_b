from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0016_story_cover_image_file_submission_cover_image_file"),
    ]

    operations = [
        migrations.AddField(
            model_name="story",
            name="epub_file",
            field=models.FileField(blank=True, null=True, upload_to="story_files/epubs/"),
        ),
        migrations.AddField(
            model_name="story",
            name="pdf_file",
            field=models.FileField(blank=True, null=True, upload_to="story_files/pdfs/"),
        ),
        migrations.AddField(
            model_name="submission",
            name="epub_file",
            field=models.FileField(blank=True, null=True, upload_to="submission_epubs/"),
        ),
    ]

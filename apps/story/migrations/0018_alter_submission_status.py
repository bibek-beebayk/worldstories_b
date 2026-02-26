from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0017_story_epub_file_story_pdf_file_submission_epub_file"),
    ]

    operations = [
        migrations.AlterField(
            model_name="submission",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("requires_edit", "Requires Edit"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]


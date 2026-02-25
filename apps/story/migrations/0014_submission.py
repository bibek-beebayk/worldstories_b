from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0013_favorite"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Submission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=256)),
                ("about", models.TextField()),
                ("content", models.TextField()),
                (
                    "story_type",
                    models.CharField(
                        choices=[
                            ("Short Story", "Short Story"),
                            ("Novel", "Novel"),
                            ("Poetry", "Poetry"),
                            ("Non Fiction", "Non Fiction"),
                        ],
                        default="Short Story",
                        max_length=50,
                    ),
                ),
                ("cover_image", models.URLField(blank=True, null=True)),
                ("notes", models.TextField(blank=True, null=True)),
                ("pdf_file", models.FileField(blank=True, null=True, upload_to="submission_pdfs/")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("reviewer_notes", models.TextField(blank=True, null=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "genres",
                    models.ManyToManyField(related_name="submissions", to="story.genre"),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reviewed_submissions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="story_submissions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]

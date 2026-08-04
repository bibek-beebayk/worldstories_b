import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("stats", "0006_filereadingprogress"),
        ("story", "0034_remove_story_original_published_date"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnalyticsEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("event_type", models.CharField(choices=[("visit", "Visit"), ("ad_impression", "Ad impression"), ("reading_session", "Reading session"), ("listening_session", "Listening session"), ("completion", "Completion"), ("download", "Download")], db_index=True, max_length=32)),
                ("visitor_id", models.CharField(db_index=True, max_length=64)),
                ("session_id", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("duration_seconds", models.FloatField(default=0)),
                ("value", models.FloatField(default=0)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("story", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="analytics_events", to="story.story")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="analytics_events", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["event_type", "created_at"], name="stats_event_type_created_idx"),
                    models.Index(fields=["visitor_id", "created_at"], name="stats_visitor_created_idx"),
                    models.Index(fields=["user", "created_at"], name="stats_user_created_idx"),
                    models.Index(fields=["story", "event_type"], name="stats_story_event_idx"),
                ],
            },
        ),
    ]

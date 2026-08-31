import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0068_audio_transcript"),
    ]

    operations = [
        migrations.CreateModel(
            name="AudioTranscriptCue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField()),
                ("start_ms", models.PositiveIntegerField()),
                ("end_ms", models.PositiveIntegerField()),
                ("text", models.TextField()),
                (
                    "audio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transcript_cues",
                        to="story.audio",
                    ),
                ),
            ],
            options={
                "ordering": ["audio", "order"],
                "indexes": [models.Index(fields=["audio", "start_ms"], name="story_cue_audio_start_idx")],
            },
        ),
        migrations.AddConstraint(
            model_name="audiotranscriptcue",
            constraint=models.CheckConstraint(
                check=models.Q(("end_ms__gt", models.F("start_ms"))),
                name="story_cue_end_after_start",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="audiotranscriptcue",
            unique_together={("audio", "order")},
        ),
    ]

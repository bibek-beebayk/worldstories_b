from django.db import migrations, models


def backfill_audio_file_sizes(apps, schema_editor):
    Audio = apps.get_model("story", "Audio")
    for audio in Audio.objects.exclude(audio_file="").iterator(chunk_size=100):
        try:
            size = audio.audio_file.size
        except Exception:
            # A missing or temporarily unavailable remote object must not
            # prevent the application deployment from completing.
            continue
        Audio.objects.filter(pk=audio.pk).update(file_size_bytes=size)


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0035_alter_story_story_type_alter_submission_story_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="audio",
            name="file_size_bytes",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.RunPython(backfill_audio_file_sizes, migrations.RunPython.noop),
    ]

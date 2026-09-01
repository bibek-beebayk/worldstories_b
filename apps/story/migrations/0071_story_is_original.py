from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0070_audio_read_along_offset_ms"),
    ]

    operations = [
        migrations.AddField(
            model_name="story",
            name="is_original",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]

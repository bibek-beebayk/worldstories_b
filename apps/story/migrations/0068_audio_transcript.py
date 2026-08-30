from django.db import migrations
import django_ckeditor_5.fields


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0067_video"),
    ]

    operations = [
        migrations.AddField(
            model_name="audio",
            name="transcript",
            field=django_ckeditor_5.fields.CKEditor5Field(
                blank=True,
                config_name="extends",
                verbose_name="Transcript",
            ),
        ),
    ]

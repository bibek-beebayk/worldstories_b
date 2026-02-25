from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0014_submission"),
    ]

    operations = [
        migrations.AddField(
            model_name="submission",
            name="published_story",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="submission",
                to="story.story",
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0018_alter_submission_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="story",
            name="is_published",
            field=models.BooleanField(default=True),
        ),
    ]


import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("story", "0069_audiotranscriptcue"),
    ]

    operations = [
        migrations.AddField(
            model_name="audio",
            name="read_along_offset_ms",
            field=models.IntegerField(
                default=0,
                validators=[
                    django.core.validators.MinValueValidator(-5000),
                    django.core.validators.MaxValueValidator(5000),
                ],
            ),
        ),
    ]

import django_ckeditor_5.fields
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0040_add_file_upload_validators"),
    ]

    operations = [
        migrations.AddField(
            model_name="story",
            name="retrospective",
            field=django_ckeditor_5.fields.CKEditor5Field(
                blank=True, null=True, verbose_name="Text"
            ),
        ),
    ]

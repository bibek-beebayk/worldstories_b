from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stats', '0025_nepalikatha_events'),
    ]

    operations = [
        migrations.AddField(
            model_name='analyticsevent',
            name='country_code',
            field=models.CharField(blank=True, db_index=True, default='', max_length=2),
        ),
        migrations.AddField(
            model_name='analyticsevent',
            name='ip_hash',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='analyticsevent',
            name='ua_family',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='analyticsevent',
            name='is_suspected_bot',
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]

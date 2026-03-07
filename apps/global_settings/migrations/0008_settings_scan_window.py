from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('global_settings', '0007_settings_day_restrictions_and_last_out'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='scan_start_time',
            field=models.TimeField(blank=True, help_text='Scanner start time window.', null=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='scan_end_time',
            field=models.TimeField(blank=True, help_text='Scanner end time window.', null=True),
        ),
    ]

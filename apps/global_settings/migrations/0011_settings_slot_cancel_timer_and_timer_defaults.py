from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('global_settings', '0010_settings_library_out_cutoff_time'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='slot_cancel_timer',
            field=models.TimeField(blank=True, help_text='Last allowed time for slot cancellation or modification.', null=True),
        ),
        migrations.AlterField(
            model_name='settings',
            name='backend_checkin_timer',
            field=models.IntegerField(blank=True, default=30, null=True),
        ),
        migrations.AlterField(
            model_name='settings',
            name='frontend_checkin_timer',
            field=models.IntegerField(blank=True, default=30, null=True),
        ),
        migrations.AlterField(
            model_name='settings',
            name='library_timer_for_hostel_out',
            field=models.IntegerField(blank=True, default=30, null=True),
        ),
    ]

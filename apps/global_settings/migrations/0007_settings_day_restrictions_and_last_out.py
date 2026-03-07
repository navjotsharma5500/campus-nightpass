from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('global_settings', '0006_settings_announcement'),
    ]

    operations = [
        migrations.RenameField(
            model_name='settings',
            old_name='last_entry_without_hostel_checkout',
            new_name='last_out_from_hostel',
        ),
        migrations.RemoveField(
            model_name='settings',
            name='valid_entry_without_hostel_checkout',
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_monday',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_tuesday',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_wednesday',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_thursday',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_friday',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_saturday',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='settings',
            name='allow_sunday',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='settings',
            name='last_out_from_hostel',
            field=models.TimeField(blank=True, null=True, verbose_name='Last Out From Hostel'),
        ),
    ]

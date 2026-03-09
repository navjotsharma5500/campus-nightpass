from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("global_settings", "0008_settings_scan_window"),
    ]

    operations = [
        migrations.AddField(
            model_name="settings",
            name="library_timer_for_hostel_out",
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
    ]

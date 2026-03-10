from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("global_settings", "0009_settings_library_timer_for_hostel_out"),
    ]

    operations = [
        migrations.AddField(
            model_name="settings",
            name="library_out_cutoff_time",
            field=models.TimeField(
                blank=True,
                null=True,
                help_text="Library OUT cutoff time. Students still inside library after this time are marked defaulter.",
            ),
        ),
    ]

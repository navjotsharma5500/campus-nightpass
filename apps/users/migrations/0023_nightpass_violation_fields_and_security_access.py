from django.db import migrations, models


def sync_role_access(apps, schema_editor):
    CustomUser = apps.get_model("users", "CustomUser")
    CustomUser.objects.filter(user_type="security").update(is_staff=True, is_superuser=False)
    CustomUser.objects.filter(user_type="admin").update(is_staff=True, is_superuser=True)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0022_security_scanner_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="nightpass",
            name="violation_code",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="nightpass",
            name="violation_time",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(sync_role_access, migrations.RunPython.noop),
    ]

from django.db import migrations, models


def set_hostel_scanner_type(apps, schema_editor):
    Security = apps.get_model('users', 'Security')
    Security.objects.filter(hostel__isnull=False).update(scanner_type='HOSTEL')


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0021_remove_security_location_and_rename'),
    ]

    operations = [
        migrations.AddField(
            model_name='security',
            name='scanner_type',
            field=models.CharField(choices=[('HOSTEL', 'Hostel'), ('LIBRARY', 'Library')], default='LIBRARY', max_length=20),
        ),
        migrations.RunPython(set_hostel_scanner_type, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='security',
            options={'verbose_name': 'Security', 'verbose_name_plural': 'Security'},
        ),
    ]

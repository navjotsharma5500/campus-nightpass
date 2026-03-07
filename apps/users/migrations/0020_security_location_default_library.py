from django.db import migrations, models


def set_library_for_entries_without_hostel(apps, schema_editor):
    Security = apps.get_model('users', 'Security')
    Security.objects.filter(hostel__isnull=True).update(location='LIBRARY')


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0019_security_location'),
    ]

    operations = [
        migrations.RunPython(set_library_for_entries_without_hostel, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='security',
            name='location',
            field=models.CharField(choices=[('HOSTEL', 'Hostel'), ('LIBRARY', 'Library')], default='LIBRARY', max_length=20),
        ),
    ]

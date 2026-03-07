from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0020_security_location_default_library'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='security',
            name='location',
        ),
        migrations.AlterModelOptions(
            name='security',
            options={'verbose_name': 'Library security', 'verbose_name_plural': 'Library security'},
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0024_alter_nightpass_pass_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='student',
            name='last_scan_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]

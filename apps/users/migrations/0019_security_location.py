from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0018_alter_student_registration_number_alter_student_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='security',
            name='location',
            field=models.CharField(choices=[('HOSTEL', 'Hostel'), ('LIBRARY', 'Library')], default='HOSTEL', max_length=20),
        ),
    ]

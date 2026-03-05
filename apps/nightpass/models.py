from django.db import models
from django.utils.translation import gettext_lazy as _

class Hostel(models.Model):
    name = models.CharField(max_length=100, unique=True, primary_key=True)
    contact_number = models.CharField(max_length=15)
    email = models.EmailField(max_length=100)

    frontend_checkin_timer = models.IntegerField(blank=True, null=True, default=0)
    backend_checkin_timer = models.IntegerField(blank=True, null=True, default=0)

    max_students_allowed = models.IntegerField(blank=True, null=True, default=0)

    def __str__(self):
        return self.name

class CampusResource(models.Model):
    # 1. Standardized choices to match your users/models.py logic
    PASS_TYPE_CHOICES = [
        ('HOSTEL', 'Starting from Hostel (5 Scans)'),
        ('OUTSIDE', 'Starting from Outside (3 Scans)'),
    ]

    name = models.CharField(max_length=100)
    description = models.TextField()
    picture = models.URLField(blank=True)
    max_capacity = models.IntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    is_booking = models.BooleanField(default=False)
    is_display = models.BooleanField(default=False)
    booking_complete = models.BooleanField(default=False)
    slots_booked = models.IntegerField(default=0, editable=False)
    
    type = models.CharField(
        max_length=100, 
        choices=[('hostel', 'Hostel'), ('location', 'Location'), ('gate', 'Gate')], 
        null=True, 
        blank=True
    )

    # 2. The Configuration Field
    # This tells the system: "If someone books THIS resource, give them a 3-scan or 5-scan pass"
    default_pass_type = models.CharField(
        max_length=20,
        choices=PASS_TYPE_CHOICES,
        default='HOSTEL',
        help_text="Set to OUTSIDE for the 3-scan Library system."
    )

    def __str__(self):
        return self.name
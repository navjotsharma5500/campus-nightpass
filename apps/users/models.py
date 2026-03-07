#models.py inside of user.py 

from datetime import timedelta
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.base_user import BaseUserManager
from django.utils.translation import gettext_lazy as _
from apps.nightpass.models import CampusResource, Hostel
from apps.global_settings.models import Settings
from django.db.models.signals import post_delete
from django.dispatch import receiver
import uuid
import requests
import os
import random
from dotenv import load_dotenv
from django.utils import timezone

load_dotenv()

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError(_('The Email must be set'))
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('user_type', 'admin')
        if extra_fields.get('is_staff') is not True:
            raise ValueError(_('Superuser must have is_staff=True.'))
        if extra_fields.get('is_superuser') is not True:
            raise ValueError(_('Superuser must have is_superuser=True.'))
        return self.create_user(email, password, **extra_fields)

class CustomUser(AbstractUser):
    unique_id = models.UUIDField(default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=100, unique=True)
    choices = (('student', 'Student'), ('admin', 'Admin'), ('security', 'Security'))
    user_type = models.CharField(max_length=20, choices=choices, default='student')
    username = None
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def has_related_object(self):
        if self.user_type == 'student':
            return hasattr(self, 'student')
        elif self.user_type == 'admin':
            return hasattr(self, 'admin')
        elif self.user_type == 'security':
            return hasattr(self, 'security')
        return False

    def save(self, *args, **kwargs):
        if self.user_type == 'security':
            self.is_staff = True
        elif self.user_type == 'admin':
            self.is_superuser = True
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

class Admin(models.Model):
    name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=15)
    designation = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    staff_id = models.CharField(max_length=20, unique=True)
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, primary_key=True)

    def __str__(self):
        return self.name

class Student(models.Model):
    name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=15, null=True, blank=True)
    registration_number = models.CharField(max_length=20,primary_key=True)
    branch = models.CharField(max_length=50, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, null=True, blank=True, choices=(('male','Male'), ('female','Female')))
    father_name = models.CharField(max_length=100, null=True, blank=True)
    mother_name = models.CharField(max_length=100, null=True, blank=True)
    course = models.CharField(max_length=50, null=True, blank=True)
    year = models.CharField(max_length=10, null=True, blank=True)
    parent_contact = models.CharField(max_length=15, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    picture = models.URLField(blank=True, null=True)
    hostel = models.ForeignKey(Hostel, on_delete=models.RESTRICT, related_name='hostel', default=None, blank=True, null=True)
    room_number = models.CharField(max_length=10, null=True, blank=True)
    has_booked = models.BooleanField(default=False)
    is_checked_in = models.BooleanField(default=True)
    hostel_checkout_time = models.DateTimeField(blank=True, null=True, editable=False)
    hostel_checkin_time = models.DateTimeField(blank=True, null=True, editable=False)
    last_checkout_time = models.DateTimeField(blank=True, null=True, editable=False)
    violation_flags = models.IntegerField(default=0)
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    email = models.EmailField(max_length=100, blank=True, null=True)

    def __str__(self):
        return str(self.registration_number)

    @property
    def status(self):
        active_pass = NightPass.objects.filter(user=self.user, valid=True).order_by('-pass_id').first()
        if not active_pass:
            return "Inside Hostel" if self.is_checked_in else "In Transit"
        return active_pass.status_message

class Security(models.Model):
    SCANNER_HOSTEL = "HOSTEL"
    SCANNER_LIBRARY = "LIBRARY"
    SCANNER_TYPE_CHOICES = (
        (SCANNER_HOSTEL, "Hostel"),
        (SCANNER_LIBRARY, "Library"),
    )

    name = models.CharField(max_length=100)
    admin_incharge = models.ForeignKey(Admin, on_delete=models.DO_NOTHING, null=True, blank=True)
    scanner_type = models.CharField(max_length=20, choices=SCANNER_TYPE_CHOICES, default=SCANNER_LIBRARY)
    hostel = models.ForeignKey(Hostel, on_delete=models.CASCADE, null=True, blank=True)
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, primary_key=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Security'
        verbose_name_plural = 'Security'

class NightPass(models.Model):
    TYPE_CHOICES = (
        ('HOSTEL', 'Starting from Hostel (5 Scans)'),
        ('OUTSIDE', 'Starting from Outside (3 Scans)'),
    )

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    pass_id = models.CharField(max_length=20, unique=True, primary_key=True, editable=False)
    pass_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='HOSTEL')

    start_time = models.TimeField()
    end_time = models.DateTimeField()
    date = models.DateField(auto_now_add=True)

    campus_resource = models.ForeignKey(CampusResource, on_delete=models.CASCADE)
    current_step = models.IntegerField(default=0, editable=False)

    hostel_checkout_time = models.DateTimeField(blank=True, null=True, editable=False)
    library_in_time = models.DateTimeField(blank=True, null=True, editable=False)
    library_out_time = models.DateTimeField(blank=True, null=True, editable=False)
    hostel_checkin_time = models.DateTimeField(blank=True, null=True, editable=False)

    valid = models.BooleanField(default=True)
    defaulter = models.BooleanField(default=False, blank=True, null=True)
    defaulter_remarks = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ('-date',)
        verbose_name_plural = 'Night Passes'

    def save(self, *args, **kwargs):
        if not self.pass_id:
            self.pass_id = f"PASS-{random.randint(1000, 99999)}"

        # FORCE the pass_type to match the Resource setting during creation
        if self.campus_resource:
            # Always sync pass_type with resource
            self.pass_type = self.campus_resource.default_pass_type

            if self._state.adding:
                if self.pass_type == 'OUTSIDE':
                    self.current_step = 1
                else:
                    self.current_step = 0

        super().save(*args, **kwargs)

    @property
    def status_message(self):
        # 1. FAIL-SAFE: If the resource itself is set to OUTSIDE, 
        # ignore the step logic and show the Library message.
        if self.pass_type == 'OUTSIDE' or self.campus_resource.default_pass_type == 'OUTSIDE':
            if not self.library_in_time:
                return "Please scan in library to activate"
            if self.library_in_time and not self.library_out_time:
                return f"Currently inside {self.campus_resource.name}"
            return "In Transit: Returning to Hostel"

        # 2. Standard Hostel Logic (5-scan system)
        if self.current_step == 0:
            return "Scan from Caretaker to activate"
        elif self.current_step == 1:
            return f"In Transit: Head to {self.campus_resource.name}"
        elif self.current_step == 2:
            return f"Currently in {self.campus_resource.name}"
        elif self.current_step == 3:
            return "In Transit: Returning to Hostel"
        
        return "Pass Completed"

    def is_late_in_transit(self):
        now = timezone.now()
        if self.current_step == 1 and self.hostel_checkout_time:
            return now > (self.hostel_checkout_time + timedelta(minutes=15))
        if self.current_step == 3 and self.library_out_time:
            return now > (self.library_out_time + timedelta(minutes=15))
        return False

@receiver(post_delete, sender=Student)
def delete_image_from_imagekit(sender, instance, **kwargs):
    endpoint = "https://api.imagekit.io/v1/files"
    private_api_key = os.getenv("Imagekit_Private_key")
    if instance.picture:
        params = {"name": instance.picture.split('/')[-1], "filetype": "image"}
        auth = (private_api_key, ":")
        response = requests.get(endpoint, params=params, auth=auth)
        if response.status_code == 200 and response.json():
            fileId = response.json()[0]['fileId']
            requests.delete(f'https://api.imagekit.io/v1/files/{fileId}', auth=auth)

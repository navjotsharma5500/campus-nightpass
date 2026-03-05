from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import CustomUser, Security

@receiver(post_save, sender=CustomUser)
def create_security_for_user(sender, instance, created, **kwargs):
    if created:
        Security.objects.create(user=instance)

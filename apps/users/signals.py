from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import CustomUser, Security

@receiver(post_save, sender=CustomUser)
def create_security_for_user(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.user_type != "security":
        return

    Security.objects.get_or_create(
        user=instance,
        defaults={"name": instance.email, "scanner_type": Security.SCANNER_LIBRARY},
    )

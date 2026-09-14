from django.core.management.base import BaseCommand

from apps.users.services.student_sync_storage import cleanup_expired


class Command(BaseCommand):
    help = "Remove expired Student Data Sync temporary previews and abandoned claims."

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {cleanup_expired()} expired student sync temporary files.")

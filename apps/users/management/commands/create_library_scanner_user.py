from django.core.management.base import BaseCommand, CommandError

from apps.users.models import CustomUser, Security


class Command(BaseCommand):
    help = "Create a library-scanner-only security user with an internal placeholder email."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Display name for the scanner user.")
        parser.add_argument("password", help="Password for the scanner user.")
        parser.add_argument(
            "--login-id",
            dest="login_id",
            help="Optional custom login id. A placeholder email will be generated from it.",
        )

    def handle(self, *args, **options):
        name = options["name"].strip()
        password = options["password"]
        login_id = (options.get("login_id") or name).strip().lower().replace(" ", ".")

        if not name:
            raise CommandError("Name is required.")
        if not login_id:
            raise CommandError("Login id could not be generated.")

        email = f"{login_id}@scanner.local"
        if CustomUser.objects.filter(email=email).exists():
            raise CommandError(f"A user with placeholder email '{email}' already exists.")

        user = CustomUser.objects.create_user(
            email=email,
            password=password,
            user_type="security",
            is_active=True,
        )

        security_profile, _ = Security.objects.get_or_create(
            user=user,
            defaults={
                "name": name,
                "scanner_type": Security.SCANNER_LIBRARY,
            },
        )
        security_profile.name = name
        security_profile.scanner_type = Security.SCANNER_LIBRARY
        security_profile.hostel = None
        security_profile.save(update_fields=["name", "scanner_type", "hostel"])

        self.stdout.write(self.style.SUCCESS("Library scanner user created successfully."))
        self.stdout.write(f"Login ID: {email}")
        self.stdout.write("User type: security")
        self.stdout.write("Scanner type: LIBRARY")

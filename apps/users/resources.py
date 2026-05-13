import logging

from import_export import resources, fields
from import_export.results import RowResult

from .models import Student, CustomUser

logger = logging.getLogger(__name__)

class StudentResource(resources.ModelResource):
    class Meta:
        model = Student
        fields = ('name', 'contact_number', 'registration_number','gender', 'branch', 'date_of_birth', 'father_name', 'mother_name', 'course', 'year', 'parent_contact', 'address', 'picture', 'hostel', 'room_number', 'email', 'user')
        import_id_fields = ('registration_number',)

    def before_import_row(self, row, **kwargs):
        registration_number = str(row.get("registration_number") or "").strip()
        email = str(row.get("email") or "").strip()
        row["registration_number"] = registration_number
        row["email"] = email

        if not registration_number:
            logger.error("Failed row: registration_number is required | row=%s", dict(row))
            raise ValueError("registration_number is required")
        if not email:
            logger.error("Failed row %s: email is required", registration_number)
            raise ValueError("Email is required to create user")

        existing_student = Student.objects.select_related("user").filter(
            registration_number=registration_number
        ).first()

        if existing_student:
            user = existing_student.user
            email_owner = CustomUser.objects.filter(email=email).exclude(pk=user.pk).first()
            if email_owner:
                logger.error(
                    "Failed row %s: email %s is already linked to another user",
                    registration_number,
                    email,
                )
                raise ValueError(f"Email {email} is already linked to another user")
            if user.email != email:
                user.email = email
            if user.user_type != "student":
                user.user_type = "student"
            user.save(update_fields=["email", "user_type", "is_staff", "is_superuser"])
        else:
            user, _ = CustomUser.objects.get_or_create(
                email=email,
                defaults={"user_type": "student", "is_active": True},
            )
            linked_student = getattr(user, "student", None)
            if linked_student and linked_student.registration_number != registration_number:
                logger.error(
                    "Failed row %s: email %s is already linked to student %s",
                    registration_number,
                    email,
                    linked_student.registration_number,
                )
                raise ValueError(
                    f"Email {email} is already linked to student {linked_student.registration_number}"
                )
            if user.user_type != "student":
                user.user_type = "student"
                user.save(update_fields=["user_type", "is_staff", "is_superuser"])

        row["user"] = user.pk
        if row.get("gender"):
            row["gender"] = str(row["gender"]).lower()

    def do_instance_save(self, instance, is_create):
        defaults = {
            field.name: getattr(instance, field.name)
            for field in self._meta.model._meta.fields
            if field.name not in ("registration_number",)
        }
        Student.objects.update_or_create(
            registration_number=instance.registration_number,
            defaults=defaults,
        )

    def after_import_row(self, row, row_result, **kwargs):
        registration_number = row.get("registration_number")
        if row_result.import_type == RowResult.IMPORT_TYPE_NEW:
            logger.info("Created student %s", registration_number)
        elif row_result.import_type == RowResult.IMPORT_TYPE_UPDATE:
            logger.info("Updated student %s", registration_number)
        elif row_result.import_type in (RowResult.IMPORT_TYPE_ERROR, RowResult.IMPORT_TYPE_INVALID):
            logger.error("Failed row %s: %s", registration_number, row_result.errors)
        return super().after_import_row(row, row_result, **kwargs)

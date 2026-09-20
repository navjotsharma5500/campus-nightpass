"""In-place student identity corrections shared by admin and bulk sync."""
from django.db import connection, transaction

from apps.users.models import Student


def change_student_registration_number(student, new_registration_number):
    old_registration_number = str(student.pk)
    new_registration_number = str(new_registration_number or "").strip()

    if (
        not new_registration_number
        or new_registration_number == old_registration_number
    ):
        return old_registration_number

    if Student.objects.filter(pk=new_registration_number).exists():
        raise ValueError(
            f"Registration number {new_registration_number} already exists."
        )

    with transaction.atomic():
        # Student.registration_number is the primary key. SQLite does not use
        # ON UPDATE CASCADE for Django foreign keys, so defer FK checks while
        # the Student PK and all direct reverse FK references move together.
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA defer_foreign_keys = ON")

        for relation in Student._meta.related_objects:
            if not (relation.one_to_many or relation.one_to_one):
                continue

            field = relation.field

            if field.target_field != Student._meta.pk:
                continue

            relation.related_model._base_manager.filter(
                **{field.attname: old_registration_number}
            ).update(
                **{field.attname: new_registration_number}
            )

        updated = Student.objects.filter(
            pk=old_registration_number
        ).update(
            registration_number=new_registration_number
        )

        if updated != 1:
            raise RuntimeError(
                "Student registration number update did not affect exactly one row."
            )

        connection.check_constraints()

    student.registration_number = new_registration_number
    return old_registration_number


"""Student administration only. Never save/delete students or touch pass state."""
import csv
import io
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from time import perf_counter

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.nightpass.models import Hostel
from apps.users.models import Admin, CustomUser, Security, Student

logger = logging.getLogger("apps.users.student_sync")
BATCH_SIZE = 500
MAX_ROWS = 20000
MAX_BYTES = 10 * 1024 * 1024
FIELDS = {
    "full": {"email", "registration_number", "name", "hostel", "room_number",
             "gender", "contact_number", "parent_contact", "year", "picture"},
    "hostel": {"email", "hostel", "room_number"},
    "picture": {"email", "picture"},
}


def normalize_email(value):
    return (value or "").strip().lower()


def read_upload(upload):
    if upload.size > MAX_BYTES:
        raise ValidationError("File exceeds 10 MB.")
    suffix = upload.name.rsplit(".", 1)[-1].lower()
    try:
        if suffix == "csv":
            rows = csv.reader(io.StringIO(upload.read().decode("utf-8-sig")), strict=True)
            return _read_rows(rows)
        if suffix == "xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(upload, read_only=True, data_only=False)
            try:
                return _read_rows(workbook.active.values)
            finally:
                workbook.close()
        raise ValidationError("Upload a UTF-8 CSV or XLSX file.")
    except (UnicodeError, csv.Error, ImportError, ValueError) as exc:
        raise ValidationError("Unable to read file. Use UTF-8 CSV (XLSX requires openpyxl).") from exc


def _read_rows(rows):
    rows = iter(rows)
    header = next(rows, None)
    if not header:
        raise ValidationError("File is empty.")
    header = [str(value or "").strip() for value in header]
    normalized = [value.lower() for value in header]
    if not all(header) or len(set(normalized)) != len(header):
        raise ValidationError("Headers must be nonblank and unique.")
    result = []
    for number, values in enumerate(rows, 2):
        values = [str(value).strip() if value is not None else "" for value in values]
        if not any(values):
            continue
        if len(values) != len(header):
            raise ValidationError(f"Row {number}: column count does not match header.")
        result.append({key: value for key, value in zip(normalized, values) if key in FIELDS["full"]})
        if len(result) > MAX_ROWS:
            raise ValidationError("Maximum 20,000 data rows per upload.")
    if not result:
        raise ValidationError("File contains no data rows.")
    return header, result


@dataclass
class Plan:
    ignored_columns: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: dict.fromkeys((
        "total_rows", "valid_rows", "duplicate_emails", "duplicate_registration_numbers",
        "missing_emails", "unknown_hostels", "conflicting_admin_security_emails",
        "students_to_create", "students_to_update", "users_to_reuse", "users_to_create",
        "skipped_rows", "error_rows", "hostel_assignments_updated", "pictures_updated",
        "assignments_to_clear", "existing_assignments", "absent_assignments",
    ), 0))
    errors: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    student_ids: set = field(default_factory=set)


def preview_sync(headers, rows, mode, clear="none", allow_blank_picture=False, lock=False):
    if mode not in FIELDS or clear not in {"none", "all", "absent"}:
        raise ValidationError("Invalid sync options.")
    if clear != "none" and mode != "hostel":
        raise ValidationError("Clearing assignments is available only in hostel mode.")
    if mode != "picture" and allow_blank_picture:
        raise ValidationError("Blank picture clearing is available only in picture mode.")
    original_headers = headers
    headers = [header.strip().lower() for header in headers]
    if not all(headers) or len(set(headers)) != len(headers):
        raise ValidationError("Headers must be nonblank and unique.")
    required = {"email"} if mode == "full" else FIELDS[mode]
    if not required.issubset(headers):
        raise ValidationError("Missing required columns: " + ", ".join(sorted(required - set(headers))))
    if not rows or len(rows) > MAX_ROWS:
        raise ValidationError("Supply between 1 and 20,000 rows.")

    users_qs = CustomUser.objects.only("id", "email", "user_type", "is_staff", "is_superuser")
    students_qs = Student.objects.all()
    if lock:
        users_qs = users_qs.select_for_update()
        students_qs = students_qs.select_for_update()
    users = list(users_qs)
    students = list(students_qs)
    by_email = defaultdict(list)
    by_student_email = defaultdict(list)
    by_user = {student.user_id: student for student in students}
    by_reg = {student.pk: student for student in students}
    for user in users:
        by_email[normalize_email(user.email)].append(user)
    for student in students:
        if student.email:
            by_student_email[normalize_email(student.email)].append(student)
    protected = set(Admin.objects.values_list("user_id", flat=True)) | set(Security.objects.values_list("user_id", flat=True))
    hostels = {hostel.name: hostel.pk for hostel in Hostel.objects.all()}
    emails = Counter(normalize_email(row.get("email")) for row in rows)
    regs = Counter(row.get("registration_number", "").strip() for row in rows) if mode == "full" else Counter()
    plan = Plan(ignored_columns=[name for name in original_headers if name.strip().lower() not in FIELDS[mode]])
    counts = plan.counts
    counts["total_rows"] = len(rows)
    counts["duplicate_emails"] = sum(n > 1 for email, n in emails.items() if email)
    counts["duplicate_registration_numbers"] = sum(n > 1 for reg, n in regs.items() if reg)
    unknown_hostels = set()
    targets = set()
    users_by_id = {user.pk: user for user in users}
    for number, row in enumerate(rows, 2):
        errors = []
        email = normalize_email(row.get("email"))
        if not email:
            counts["missing_emails"] += 1
            errors.append("email is required")
        else:
            try:
                CustomUser._meta.get_field("email").clean(email, None)
            except ValidationError:
                errors.append("invalid email (maximum 100 characters)")
        if email and emails[email] > 1:
            errors.append(f"duplicate email: {email}")
        matches = by_email.get(email, [])
        student_matches = by_student_email.get(email, [])
        user = matches[0] if len(matches) == 1 else None
        student = by_user.get(user.pk) if user else None
        if len(matches) > 1 or len(student_matches) > 1:
            errors.append("ambiguous existing email; correct duplicate database emails first")
        if student_matches:
            profile = student_matches[0]
            if student and student.pk != profile.pk:
                errors.append("email identifies different user and student records")
            elif user and profile.user_id != user.pk:
                errors.append("student email belongs to another linked user")
            else:
                student = profile
                user = users_by_id[profile.user_id]
                if normalize_email(user.email) != email:
                    errors.append("student and linked user emails disagree; correct them before syncing")
        if any(u.user_type != "student" or u.is_staff or u.is_superuser or u.pk in protected
               for u in matches + ([user] if user else [])):
            counts["conflicting_admin_security_emails"] += 1
            errors.append(f"protected admin/security/non-student email: {email}")
        if mode != "full" and not student:
            errors.append(f"no existing student for {email}")
        values = {}
        for key in (set(headers) & FIELDS[mode]) - {"email", "registration_number"}:
            value = row.get(key, "").strip()
            if key == "picture" and not value and not allow_blank_picture:
                continue
            if key == "hostel":
                if value and value not in hostels:
                    unknown_hostels.add(value)
                    errors.append(f"unknown hostel: {value}")
                values["hostel_id"] = hostels.get(value)
                continue
            if key == "gender":
                value = {"m": "male", "f": "female"}.get(value.lower(), value.lower())
            model_field = Student._meta.get_field(key)
            value = value or (None if model_field.null else "")
            try:
                model_field.clean(value, None)
            except ValidationError as exc:
                errors.append(f"{key}: {'; '.join(exc.messages)}")
            values[key] = value
        if mode == "full":
            values["email"] = email
            reg = row.get("registration_number", "").strip()
            if reg and regs[reg] > 1:
                errors.append(f"duplicate registration number: {reg}")
            if reg and reg in by_reg and (not student or student.pk != reg):
                errors.append(f"registration number already belongs to another student: {reg}")
            if student and "registration_number" in headers and reg != student.pk:
                errors.append("registration number is the primary key and cannot be changed by sync")
            if not student:
                try:
                    Student._meta.get_field("registration_number").clean(reg, None)
                    Student._meta.get_field("name").clean(values.get("name", ""), None)
                except ValidationError as exc:
                    errors.append("new students require valid registration_number and name: " + "; ".join(exc.messages))
                values["registration_number"] = reg
        if student:
            if student.pk in targets:
                errors.append("multiple rows target the same student")
            targets.add(student.pk)
            plan.student_ids.add(student.pk)
        if errors:
            counts["error_rows"] += 1
            plan.errors.extend(f"Row {number}: {message}" for message in errors)
            continue
        counts["valid_rows"] += 1
        # Only actual differences are written; after clear-all restore supplied values.
        changed = {key: value for key, value in values.items()
                   if not student or getattr(student, key) != value
                   or (clear == "all" and key in {"hostel_id", "room_number"})}
        if student:
            counts["students_to_update"] += bool(changed)
        else:
            counts["students_to_create"] += 1
            counts["users_to_reuse" if user else "users_to_create"] += 1
        counts["hostel_assignments_updated"] += bool({"hostel_id", "room_number"} & changed.keys())
        counts["pictures_updated"] += "picture" in changed
        if not changed:
            counts["skipped_rows"] += 1
        plan.entries.append((student, user, email, changed))
    counts["unknown_hostels"] = len(unknown_hostels)
    counts["skipped_rows"] += counts["error_rows"]
    assigned = [s for s in students if s.hostel_id is not None or s.room_number is not None]
    counts["existing_assignments"] = len(assigned)
    counts["absent_assignments"] = sum(s.pk not in plan.student_ids for s in assigned)
    counts["assignments_to_clear"] = (len(assigned) if clear == "all" else
                                       counts["absent_assignments"] if clear == "absent" else 0)
    return plan


def apply_sync(headers, rows, mode, *, actor, filename, clear="none", allow_blank_picture=False):
    started = perf_counter()
    try:
        with transaction.atomic():
            # Serialize this feature across Gunicorn workers on PostgreSQL. SQLite
            # serializes writers itself; a competing write fails and rolls back.
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(1937012083)")
            plan = preview_sync(headers, rows, mode, clear, allow_blank_picture, lock=True)
            if plan.errors:
                raise ValidationError(plan.errors)
            if clear != "none":
                queryset = Student.objects.all()
                if clear == "absent":
                    # Email matching above resolves the stable primary keys first.
                    queryset = queryset.exclude(pk__in=plan.student_ids)
                queryset.update(hostel=None, room_number=None)
            new_users = []
            for student, user, email, values in plan.entries:
                if not student and not user:
                    user = CustomUser(email=email, user_type="student")
                    user.set_unusable_password()
                    new_users.append(user)
            CustomUser.objects.bulk_create(new_users, batch_size=BATCH_SIZE)
            # Reload IDs for database backends without bulk INSERT RETURNING.
            new_by_email = {}
            for start in range(0, len(new_users), BATCH_SIZE):
                emails = [u.email for u in new_users[start:start + BATCH_SIZE]]
                new_by_email.update((u.email, u) for u in CustomUser.objects.filter(email__in=emails))
            creates = []
            updates = defaultdict(list)
            for student, user, email, values in plan.entries:
                if student:
                    if values:
                        for key, value in values.items():
                            setattr(student, key, value)
                        updates[tuple(sorted(values))].append(student)
                else:
                    creates.append(Student(user=user or new_by_email[email], **values))
            Student.objects.bulk_create(creates, batch_size=BATCH_SIZE)
            for fields, objects in updates.items():
                # Bound CASE expression memory as well as SQL batch size.
                for start in range(0, len(objects), BATCH_SIZE):
                    Student.objects.bulk_update(objects[start:start + BATCH_SIZE], fields, batch_size=BATCH_SIZE)
        counts = plan.counts
        duration = perf_counter() - started
        logger.info("Student sync admin=%s mode=%s filename=%r total=%s created=%s updated=%s users_created=%s users_reused=%s skipped=%s failed=0 timestamp=%s duration=%.3fs",
                    actor, mode, filename, len(rows), counts["students_to_create"], counts["students_to_update"],
                    counts["users_to_create"], counts["users_to_reuse"], counts["skipped_rows"], timezone.now().isoformat(), duration)
        return counts, duration
    except Exception:
        logger.warning("Student sync rolled back admin=%s mode=%s filename=%r total=%s created=0 updated=0 failed=%s timestamp=%s duration=%.3fs",
                       actor, mode, filename, len(rows), len(rows), timezone.now().isoformat(), perf_counter() - started)
        raise

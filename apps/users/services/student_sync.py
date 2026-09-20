"""Student administration only. Never delete students or touch pass state."""
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
PICTURE_HEADERS = {"picture", "url", "picture_url", "image_url"}
FIELDS = {
    "full": {"email", "registration_number", "name", "hostel", "room_number",
             "gender", "contact_number", "parent_contact", "year", "picture"},
    "hostel": {"email", "hostel", "room_number"},
    "picture": {"email", "picture"},
    "identity": {"user", "registration_number", "email"},
}


def normalize_email(value):
    return (value or "").strip().lower()


def normalize_headers(headers):
    normalized = [header.strip().lower() for header in headers]
    picture_sources = [header for header, name in zip(headers, normalized) if name in PICTURE_HEADERS]
    if len(picture_sources) > 1:
        raise ValidationError("Ambiguous picture columns: " + ", ".join(picture_sources)
                              + ". Supply only one of picture, url, picture_url, image_url.")
    if not all(normalized) or len(set(normalized)) != len(normalized):
        raise ValidationError("Headers must be nonblank and unique.")
    return ["picture" if name in PICTURE_HEADERS else name for name in normalized]


def read_upload(upload, mode="full"):
    if upload.size > MAX_BYTES:
        raise ValidationError("File exceeds 10 MB.")
    suffix = upload.name.rsplit(".", 1)[-1].lower()
    try:
        if suffix == "csv":
            rows = csv.reader(io.StringIO(upload.read().decode("utf-8-sig")), strict=True)
            return _read_rows(rows, mode)
        if suffix == "xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(upload, read_only=True, data_only=False)
            try:
                return _read_rows(workbook.active.values, mode)
            finally:
                workbook.close()
        raise ValidationError("Upload a UTF-8 CSV or XLSX file.")
    except (UnicodeError, csv.Error, ImportError, ValueError) as exc:
        raise ValidationError("Unable to read file. Use UTF-8 CSV (XLSX requires openpyxl).") from exc


def _read_rows(rows, mode="full"):
    rows = iter(rows)
    header = next(rows, None)
    if not header:
        raise ValidationError("File is empty.")
    header = [str(value or "").strip() for value in header]
    normalized = normalize_headers(header)
    result = []
    for number, values in enumerate(rows, 2):
        values = [str(value).strip() if value is not None else "" for value in values]
        if not any(values):
            values = [""] * len(header)
        if len(values) != len(header):
            raise ValidationError(f"Row {number}: column count does not match header.")
        result.append({key: value for key, value in zip(normalized, values) if key in FIELDS["full"] | ({"user"} if mode == "identity" else set())})
        if len(result) > MAX_ROWS:
            raise ValidationError("Maximum 20,000 data rows per upload.")
    if not result:
        raise ValidationError("File contains no data rows.")
    return header, result


@dataclass
class Plan:
    ignored_columns: list = field(default_factory=list)
    picture_source: str = ""
    counts: dict = field(default_factory=lambda: dict.fromkeys((
        "total_rows", "rows_considered", "valid_rows", "unchanged_rows",
        "identical_duplicate_rows_skipped", "footer_rows_skipped", "duplicate_emails",
        "duplicate_registration_numbers",
        "missing_emails", "unknown_hostels", "conflicting_admin_security_emails",
        "students_to_create", "students_to_update", "users_to_reuse", "users_to_create",
        "skipped_rows", "error_rows", "hostel_assignments_updated", "pictures_updated",
        "assignments_to_clear", "existing_assignments", "absent_assignments",
    ), 0))
    errors: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    identity_rows: list = field(default_factory=list)
    student_ids: set = field(default_factory=set)


def preview_sync(headers, rows, mode, clear="none", allow_blank_picture=False, lock=False):
    if mode not in FIELDS or clear not in {"none", "all", "absent"}:
        raise ValidationError("Invalid sync options.")
    if clear != "none" and mode != "hostel":
        raise ValidationError("Clearing assignments is available only in hostel mode.")
    if mode != "picture" and allow_blank_picture:
        raise ValidationError("Blank picture clearing is available only in picture mode.")
    original_headers = headers
    headers = normalize_headers(headers)
    required = {"email"} if mode == "full" else FIELDS[mode]
    if not required.issubset(headers):
        raise ValidationError("Missing required columns: " + ", ".join(sorted(required - set(headers))))
    if not rows or len(rows) > MAX_ROWS:
        raise ValidationError("Supply between 1 and 20,000 rows.")

    if mode == "identity":
        return _preview_identity(original_headers, headers, rows, lock=lock)

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
    plan = Plan(ignored_columns=[source for source, name in zip(original_headers, headers) if name not in FIELDS[mode]])
    if "picture" in FIELDS[mode] and "picture" in headers:
        plan.picture_source = original_headers[headers.index("picture")]
    counts = plan.counts
    counts["total_rows"] = len(rows)
    # Keep physical positions, and inspect identity before mode-specific filtering.
    candidates = []
    groups = defaultdict(list)
    effective_fields = sorted(set(headers) & FIELDS[mode])
    for number, row in enumerate(rows, 2):
        email = normalize_email(row.get("email"))
        if not email and not any(row.get(key, "").strip() for key in ("registration_number", "name")):
            counts["footer_rows_skipped"] += 1
            continue
        effective = []
        for key in effective_fields:
            value = row.get(key, "").strip()
            if key == "email":
                value = email
            elif key == "gender":
                value = {"m": "male", "f": "female"}.get(value.lower(), value.lower())
            effective.append(value)
        candidates.append((number, row, email))
        if email:
            groups[email].append((number, tuple(effective)))
    conflicts = {email: [number for number, _ in group]
                 for email, group in groups.items() if len({values for _, values in group}) > 1}
    retained = []
    seen = set()
    for number, row, email in candidates:
        if email and email in seen and email not in conflicts:
            counts["identical_duplicate_rows_skipped"] += 1
            continue
        seen.add(email)
        retained.append((number, row))
    counts["rows_considered"] = len(retained)
    counts["duplicate_emails"] = len(conflicts)
    regs = Counter(row.get("registration_number", "").strip() for _, row in retained) if mode == "full" else Counter()
    counts["duplicate_registration_numbers"] = sum(n > 1 for reg, n in regs.items() if reg)
    unknown_hostels = set()
    targets = set()
    users_by_id = {user.pk: user for user in users}
    for number, row in retained:
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
        if email in conflicts:
            positions = ", ".join(map(str, conflicts[email]))
            errors.append(f"conflicting duplicate email: {email} (rows {positions})")
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
            counts["unchanged_rows"] += 1
        plan.entries.append((student, user, email, changed))
    counts["unknown_hostels"] = len(unknown_hostels)
    counts["skipped_rows"] = (counts["unchanged_rows"] + counts["error_rows"]
                              + counts["identical_duplicate_rows_skipped"] + counts["footer_rows_skipped"])
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
            if mode == "identity" and connection.vendor == "postgresql":
                # Row locks cannot protect an absent email, and the existing
                # unique email constraint is case-sensitive. Block concurrent
                # identity/protection inserts and writes until commit as well.
                tables = sorted(connection.ops.quote_name(model._meta.db_table)
                                for model in (CustomUser, Student, Admin, Security))
                with connection.cursor() as cursor:
                    cursor.execute("LOCK TABLE " + ", ".join(tables) + " IN SHARE ROW EXCLUSIVE MODE")
            plan = preview_sync(headers, rows, mode, clear, allow_blank_picture, lock=True)
            if plan.errors:
                raise ValidationError(plan.errors)
            if mode == "identity":
                _apply_identity(plan)
            else:
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


def _preview_identity(original_headers, headers, rows, *, lock=False):
    # Lock in a consistent order, including possible existing target owners.
    users_qs = CustomUser.objects.order_by("pk")
    students_qs = Student.objects.order_by("pk")
    admins = Admin.objects.order_by("pk")
    security = Security.objects.order_by("pk")
    if lock:
        users_qs = users_qs.select_for_update()
        students_qs = students_qs.select_for_update()
        admins = admins.select_for_update()
        security = security.select_for_update()
    users = {u.pk: u for u in users_qs}
    students = list(students_qs)
    protected = {a.user_id for a in admins} | {s.user_id for s in security}
    profiles = defaultdict(list)
    emails = defaultdict(set)
    rolls = {s.pk: s for s in students}
    for student in students:
        profiles[student.user_id].append(student)
    for user in users.values():
        emails[normalize_email(user.email)].add(user.pk)
    normalized = []
    for row in rows:
        raw_user = str(row.get("user", "")).strip()
        digits = raw_user.lstrip("0") or "0"
        user_id = int(digits) if digits.isascii() and digits.isdecimal() and len(digits) <= 19 else None
        normalized.append((user_id, str(row.get("registration_number", "")).strip(),
                           normalize_email(row.get("email"))))
    user_counts = Counter(item[0] for item in normalized)
    roll_counts = Counter(item[1] for item in normalized)
    email_counts = Counter(item[2] for item in normalized)
    plan = Plan(ignored_columns=[source for source, name in zip(original_headers, headers)
                                if name not in FIELDS["identity"]])
    counts = plan.counts
    counts.update(total_rows=len(rows), rows_considered=len(rows), identity_rows_to_update=0,
                  identity_rows_unchanged=0, identity_error_rows=0,
                  duplicate_users=sum(n > 1 for key, n in user_counts.items() if key is not None))
    counts["duplicate_registration_numbers"] = sum(n > 1 for key, n in roll_counts.items() if key)
    counts["duplicate_emails"] = sum(n > 1 for key, n in email_counts.items() if key)
    for number, (user_id, reg, email) in enumerate(normalized, 2):
        errors = []
        user = users.get(user_id)
        matches = profiles.get(user_id, [])
        student = matches[0] if len(matches) == 1 else None
        if not user:
            errors.append("user must be an existing CustomUser.id")
        else:
            if user.user_type != "student" or user.is_staff or user.is_superuser or user_id in protected:
                errors.append("protected admin/security/non-student user")
                counts["conflicting_admin_security_emails"] += 1
            if not student:
                errors.append("user must have exactly one Student profile")
        for field_name, value, model in (("registration_number", reg, Student), ("email", email, CustomUser)):
            try:
                model._meta.get_field(field_name).clean(value, None)
            except ValidationError as exc:
                errors.append(f"{field_name}: {'; '.join(exc.messages)}")
        if not email:
            counts["missing_emails"] += 1
        if user_id is not None and user_counts[user_id] > 1:
            errors.append(f"duplicate user: {user_id}")
        if reg and roll_counts[reg] > 1:
            errors.append(f"duplicate target registration number: {reg}")
        if email and email_counts[email] > 1:
            errors.append(f"duplicate target email: {email}")
        if reg in rolls and (not student or rolls[reg].pk != student.pk):
            errors.append(f"registration number already belongs to another student: {reg}")
        if emails[email] - {user_id}:
            errors.append(f"email already belongs to another CustomUser: {email}")
        changed = bool(student and user and (student.pk != reg or student.email != email or user.email != email))
        plan.identity_rows.append(dict(row=number, user=user_id, current_registration_number=student.pk if student else "",
                                       registration_number=reg, current_email=user.email if user else "",
                                       current_student_email=student.email if student else "", email=email,
                                       status="Error" if errors else "Update" if changed else "Unchanged"))
        if errors:
            counts["error_rows"] += 1
            plan.errors.extend(f"Row {number}: {message}" for message in errors)
            continue
        counts["valid_rows"] += 1
        counts["students_to_update"] += changed
        counts["unchanged_rows"] += not changed
        plan.entries.append((student, user, reg, email, changed))
    counts["identity_rows_to_update"] = counts["students_to_update"]
    counts["identity_rows_unchanged"] = counts["unchanged_rows"]
    counts["identity_error_rows"] = counts["error_rows"]
    counts["skipped_rows"] = counts["error_rows"] + counts["unchanged_rows"]
    return plan


def _apply_identity(plan):
    from .student_identity import change_student_registration_number

    for student, user, reg, email, changed in plan.entries:
        if not changed:
            continue
        if student.pk != reg:
            # check_constraints makes PostgreSQL constraints immediate; defer
            # again for each correction within this upload's outer transaction.
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            change_student_registration_number(student, reg)
        if CustomUser.objects.filter(pk=user.pk).update(email=email) != 1:
            raise ValidationError("Identity update lost its CustomUser.")
        if Student.objects.filter(pk=reg, user_id=user.pk).update(email=email) != 1:
            raise ValidationError("Identity update lost its Student profile.")
    connection.check_constraints()

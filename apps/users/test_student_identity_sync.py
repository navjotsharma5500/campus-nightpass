"""Identity corrections must preserve records and reject the whole invalid upload."""
from datetime import time
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.nightpass.models import CampusResource
from apps.users.models import Admin, CustomUser, NightPass, Security, Student, ViolationAuditLog
from apps.users.services.student_sync import apply_sync, preview_sync, read_upload
from apps.users.services.student_identity import change_student_registration_number


class IdentitySyncTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user("old@example.com", None)
        self.student = Student.objects.create(user=self.user, registration_number="001", name="Original",
                                              email=self.user.email, picture="https://example.com/photo.jpg",
                                              violation_flags=3, has_booked=True, is_checked_in=False,
                                              last_scan_at=timezone.now())
        resource = CampusResource.objects.create(name="Library", max_capacity=10, start_time=time(20), end_time=time(23))
        self.nightpass = NightPass.objects.create(user=self.user, campus_resource=resource, start_time=time(20),
                                                 end_time=timezone.now(), defaulter=True, violation_code="LATE")
        self.audit = ViolationAuditLog.objects.create(student=self.student, night_pass=self.nightpass,
                                                     event_type="BECAME_DEFAULTER", message="Original history")

    def row(self, **kwargs):
        return {"user": str(self.user.pk), "registration_number": "001", "email": self.user.email, **kwargs}

    def apply(self, rows):
        return apply_sync(list(rows[0]), rows, "identity", actor="test-admin", filename="identity.csv")

    def snapshot(self):
        return [list(model.objects.order_by("pk").values())
                for model in (CustomUser, Student, NightPass, ViolationAuditLog)]

    def assert_rejected(self, rows, message):
        before = self.snapshot()
        plan = preview_sync(list(rows[0]), rows, "identity")
        self.assertTrue(any(message in error for error in plan.errors), plan.errors)
        with self.assertRaises(ValidationError):
            self.apply(rows)
        self.assertEqual(before, self.snapshot())

    def check_change(self, reg, email):
        before_student = Student.objects.values().get(pk="001")
        before_user = CustomUser.objects.values().get(pk=self.user.pk)
        before_pass = NightPass.objects.values().get(pk=self.nightpass.pk)
        before_audit = ViolationAuditLog.objects.values().get(pk=self.audit.pk)
        with patch.object(Student, "save", side_effect=AssertionError("must not save/recreate student")), \
             patch.object(CustomUser, "save", side_effect=AssertionError("must not save/recreate user")), \
             patch("requests.get", side_effect=AssertionError("must preserve image")):
            counts, _ = self.apply([self.row(registration_number=reg, email=email)])
        before_student.update(registration_number=reg, email=email)
        before_user.update(email=email)
        before_audit.update(student_id=reg)
        self.assertEqual(Student.objects.values().get(user_id=self.user.pk), before_student)
        self.assertEqual(CustomUser.objects.values().get(pk=self.user.pk), before_user)
        self.assertEqual(NightPass.objects.values().get(pk=self.nightpass.pk), before_pass)
        self.assertEqual(ViolationAuditLog.objects.values().get(pk=self.audit.pk), before_audit)
        self.assertEqual(NightPass.objects.get(pk=self.nightpass.pk).user.student.pk, reg)
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(CustomUser.objects.count(), 1)
        self.assertEqual(counts["identity_rows_to_update"], 1)

    def test_roll_only(self):
        self.check_change("002", "old@example.com")

    def test_email_only(self):
        self.check_change("001", "new@example.com")

    def test_roll_and_email(self):
        self.check_change("002", "new@example.com")

    def test_unchanged(self):
        before = self.snapshot()
        counts, _ = self.apply([self.row()])
        self.assertEqual(counts["identity_rows_unchanged"], 1)
        self.assertEqual(counts["identity_rows_to_update"], 0)
        self.assertEqual(before, self.snapshot())

    def test_preview_is_read_only_and_shows_current_and_requested(self):
        row = self.row(registration_number="002", email="new@example.com")
        with CaptureQueriesContext(connection) as queries:
            plan = preview_sync(list(row), [row], "identity")
        self.assertTrue(all(q["sql"].startswith("SELECT") for q in queries))
        self.assertEqual(plan.identity_rows[0]["current_registration_number"], "001")
        self.assertEqual(plan.identity_rows[0]["registration_number"], "002")
        self.assertEqual(plan.identity_rows[0]["current_email"], "old@example.com")
        self.assertEqual(plan.identity_rows[0]["email"], "new@example.com")

    def other(self):
        user = CustomUser.objects.create_user("other@example.com", None)
        Student.objects.create(user=user, registration_number="OTHER", email=user.email, name="Other")
        return user

    def test_duplicate_user_including_identical_rows(self):
        self.assert_rejected([self.row(), self.row(user=f"00{self.user.pk}")], "duplicate user")

    def test_duplicate_target_roll(self):
        other = self.other()
        self.assert_rejected([self.row(registration_number="NEW"), self.row(user=str(other.pk), registration_number="NEW", email=other.email)], "duplicate target registration")

    def test_existing_target_roll(self):
        self.other()
        self.assert_rejected([self.row(registration_number="OTHER")], "already belongs to another student")

    def test_duplicate_target_email_case_insensitive(self):
        other = self.other()
        self.assert_rejected([self.row(email=" NEW@example.com "), self.row(user=str(other.pk), registration_number="OTHER", email="new@EXAMPLE.COM")], "duplicate target email")

    def test_existing_target_email_case_insensitive(self):
        CustomUser.objects.create_user("Taken@example.com", None)
        self.assert_rejected([self.row(email="TAKEN@EXAMPLE.COM")], "already belongs to another CustomUser")

    def test_missing_invalid_and_profileless_user(self):
        no_profile = CustomUser.objects.create_user("profileless@example.com", None)
        for value in ("", "not-an-id", "1.0", "99999999", str(no_profile.pk)):
            with self.subTest(value=value):
                self.assert_rejected([self.row(user=value)], "user")

    def test_protected_users(self):
        for flags in ({"user_type": "admin"}, {"user_type": "security"}, {"user_type": "other"}, {"is_staff": True}, {"is_superuser": True}):
            with self.subTest(flags=flags):
                CustomUser.objects.filter(pk=self.user.pk).update(**dict({"user_type": "student", "is_staff": False, "is_superuser": False}, **flags))
                self.assert_rejected([self.row()], "protected")

    def test_protected_profiles_even_with_student_role(self):
        for model in (Admin, Security):
            with self.subTest(model=model):
                profile = model.objects.create(user=self.user)
                self.assert_rejected([self.row()], "protected")
                profile.delete()

    def test_blank_and_invalid_identity_values(self):
        for field, value in (("registration_number", ""), ("email", ""), ("email", "invalid"), ("registration_number", "x" * 21)):
            with self.subTest(field=field, value=value):
                self.assert_rejected([self.row(**{field: value})], field)

    def test_required_columns(self):
        for field in ("user", "registration_number", "email"):
            row = self.row()
            del row[field]
            with self.subTest(field=field), self.assertRaisesMessage(ValidationError, "Missing required columns"):
                preview_sync(list(row), [row], "identity")

    def test_invalid_second_row_prevents_all_updates(self):
        self.assert_rejected([self.row(registration_number="NEW"), self.row(user="999999", registration_number="SECOND", email="missing@example.com")], "existing CustomUser")

    def test_runtime_failure_rolls_back_prior_correction_and_history(self):
        other = self.other()
        before = self.snapshot()
        def fail_second(student, reg):
            if student.user_id == other.pk:
                raise RuntimeError("injected failure")
            return change_student_registration_number(student, reg)
        with patch("apps.users.services.student_identity.change_student_registration_number", side_effect=fail_second):
            with self.assertRaisesMessage(RuntimeError, "injected failure"):
                self.apply([self.row(registration_number="NEW", email="new@example.com"), self.row(user=str(other.pk), registration_number="SECOND", email=other.email)])
        self.assertEqual(before, self.snapshot())

    def test_apply_revalidates_after_preview(self):
        rows = [self.row(email="new@example.com")]
        self.assertFalse(preview_sync(list(rows[0]), rows, "identity").errors)
        CustomUser.objects.create_user("NEW@example.com", None)
        self.assert_rejected(rows, "already belongs")

    def test_full_still_refuses_roll_changes(self):
        row = self.row(registration_number="NEW")
        with self.assertRaisesMessage(ValidationError, "cannot be changed by sync"):
            apply_sync(list(row), [row], "full", actor="test", filename="full.csv")

    def test_csv_preserves_user_column(self):
        headers, rows = read_upload(SimpleUploadedFile("identity.csv", f"user,registration_number,email\n{self.user.pk},NEW,new@example.com\n".encode()), mode="identity")
        self.assertEqual(rows[0]["user"], str(self.user.pk))
        self.assertFalse(preview_sync(headers, rows, "identity").errors)

    def test_two_roll_corrections_with_history(self):
        other = self.other()
        audit = ViolationAuditLog.objects.create(student_id="OTHER", event_type="ALLOWED_AGAIN", message="Other history")
        counts, _ = self.apply([self.row(registration_number="NEW"), self.row(user=str(other.pk), registration_number="SECOND", email=other.email)])
        self.audit.refresh_from_db()
        audit.refresh_from_db()
        self.assertEqual(self.audit.student_id, "NEW")
        self.assertEqual(audit.student_id, "SECOND")
        self.assertEqual(counts["identity_rows_to_update"], 2)

    def test_individual_admin_uses_shared_correction(self):
        from apps.users.admin import StudentAdmin
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from types import SimpleNamespace
        model_admin = StudentAdmin(Student, AdminSite())
        request = RequestFactory().post("/")
        request.user = CustomUser.objects.create_superuser("admin@example.com", None)
        form = SimpleNamespace(cleaned_data={"new_registration_number": "NEW", "email": self.user.email})
        with patch("apps.users.admin.messages.success"):
            model_admin.save_model(request, self.student, form, change=True)
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.student_id, "NEW")
        self.assertEqual(Student.objects.get(pk="NEW").user_id, self.user.pk)
        self.assertEqual(NightPass.objects.get(pk=self.nightpass.pk).user.student.pk, "NEW")

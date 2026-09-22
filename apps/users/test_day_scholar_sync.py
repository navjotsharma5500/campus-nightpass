"""Student type sync is additive; identity and narrow sync modes retain their scope."""
from datetime import time, timedelta
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from tablib import Dataset

from apps.nightpass.models import CampusResource, Hostel
from apps.users.admin import StudentResource as AdminStudentResource
from apps.users.models import CustomUser, NightPass, Student
from apps.users.resources import StudentResource
from apps.users.services.student_sync import apply_sync, read_upload


class DayScholarSyncTests(TestCase):
    def setUp(self):
        self.hostel = Hostel.objects.create(name="A", email="a@example.com", contact_number="123")
        self.user = CustomUser.objects.create_user("student@example.com", None)
        self.student = Student.objects.create(user=self.user, email=self.user.email, registration_number="001",
            name="Original", hostel=self.hostel, room_number="101", picture="https://example.com/p.jpg",
            violation_flags=2, has_booked=True)

    def sync(self, row, mode="full"):
        return apply_sync(list(row), [row], mode, actor="test", filename="types.csv")

    def test_full_sync_normalizes_values_and_clears_stale_assignment(self):
        for value, expected in [("hosteller", "HOSTELLER"), ("HOSTELLER", "HOSTELLER"),
                ("hostler", "HOSTELLER"), ("day scholar", "DAY_SCHOLAR"),
                ("DAY_SCHOLAR", "DAY_SCHOLAR"), ("day-scholar", "DAY_SCHOLAR")]:
            with self.subTest(value=value):
                self.sync({"email": self.user.email, "student_type": value, "hostel": "A", "room_number": "101"})
                self.student.refresh_from_db()
                self.assertEqual(self.student.student_type, expected)
                self.assertEqual(self.student.hostel_id, None if expected == "DAY_SCHOLAR" else "A")
                self.assertEqual(self.student.room_number, None if expected == "DAY_SCHOLAR" else "101")

    def test_explicit_day_scholar_ignores_irrelevant_unknown_hostel(self):
        self.sync({"email": self.user.email, "student_type": "day scholar", "hostel": "Obsolete hostel"})
        self.student.refresh_from_db()
        self.assertIsNone(self.student.hostel_id)
        self.assertIsNone(self.student.room_number)

    def test_absent_column_preserves_existing_type_and_defaults_new_students(self):
        self.sync({"email": self.user.email, "student_type": "DAY_SCHOLAR"})
        self.sync({"email": self.user.email, "name": "Updated"})
        self.student.refresh_from_db()
        self.assertEqual(self.student.student_type, "DAY_SCHOLAR")
        self.sync({"email": "new@example.com", "registration_number": "002", "name": "New"})
        self.assertEqual(Student.objects.get(pk="002").student_type, "HOSTELLER")

    def test_new_day_scholar_without_hostel(self):
        self.sync({"email": "new@example.com", "registration_number": "002", "name": "New", "student_type": "day-scholar"})
        student = Student.objects.get(pk="002")
        self.assertEqual(student.student_type, "DAY_SCHOLAR")
        self.assertIsNone(student.hostel_id)

    def test_invalid_or_blank_type_rolls_back(self):
        before = Student.objects.values().get(pk=self.student.pk)
        for value in ("", "unknown"):
            with self.assertRaises(ValidationError):
                self.sync({"email": self.user.email, "student_type": value, "name": "Changed"})
            self.assertEqual(Student.objects.values().get(pk=self.student.pk), before)

    def test_type_update_preserves_ids_history_picture_and_pass_state(self):
        resource = CampusResource.objects.create(name="Library", description="L", max_capacity=10,
            start_time=time(0), end_time=time(23))
        user_pass = NightPass.objects.create(user=self.user, campus_resource=resource,
            start_time=time(20), end_time=timezone.now()+timedelta(hours=1), violation_code="LATE_HOSTEL_IN")
        before_pass = NightPass.objects.values().get(pk=user_pass.pk)
        before_user = CustomUser.objects.values().get(pk=self.user.pk)
        self.sync({"email": self.user.email, "student_type": "DAY_SCHOLAR"})
        self.student.refresh_from_db()
        self.assertEqual(self.student.pk, "001")
        self.assertEqual(self.student.user_id, self.user.pk)
        self.assertEqual(self.student.picture, "https://example.com/p.jpg")
        self.assertEqual(self.student.violation_flags, 2)
        self.assertTrue(self.student.has_booked)
        self.assertEqual(CustomUser.objects.values().get(pk=self.user.pk), before_user)
        self.assertEqual(NightPass.objects.values().get(pk=user_pass.pk), before_pass)

    def test_narrow_sync_modes_ignore_student_type(self):
        for mode, row in [("hostel", {"hostel": "A", "room_number": "202"}),
                          ("picture", {"picture": "https://example.com/new.jpg"}),
                          ("identity", {"user": str(self.user.pk), "registration_number": "001"})]:
            with self.subTest(mode=mode):
                before = Student.objects.values().get(pk=self.student.pk)
                self.sync({"email": self.user.email, "student_type": "DAY_SCHOLAR", **row}, mode)
                self.student.refresh_from_db()
                self.assertEqual(self.student.student_type, "HOSTELLER")
                self.assertEqual(self.student.user_id, self.user.pk)
                if mode != "hostel":
                    self.assertEqual(self.student.hostel_id, before["hostel_id"])
                    self.assertEqual(self.student.room_number, before["room_number"])

    def test_csv_and_xlsx_reader_accept_last_student_type_column(self):
        from openpyxl import Workbook
        headers = ["email", "registration_number", "name", "student_type"]
        values = ["new@example.com", "002", "New", "day scholar"]
        book = Workbook(); book.active.append(headers); book.active.append(values)
        output = BytesIO(); book.save(output)
        for name, content in [("types.csv", (",".join(headers)+"\n"+",".join(values)).encode()), ("types.xlsx", output.getvalue())]:
            parsed_headers, rows = read_upload(SimpleUploadedFile(name, content))
            self.assertEqual(parsed_headers[-1], "student_type")
            self.assertEqual(rows[0]["student_type"], "day scholar")

    def test_both_student_exports_end_with_canonical_student_type(self):
        for resource_class in (StudentResource, AdminStudentResource):
            for value in ("HOSTELLER", "DAY_SCHOLAR"):
                Student.objects.filter(pk=self.student.pk).update(student_type=value)
                dataset = resource_class().export(Student.objects.filter(pk=self.student.pk))
                self.assertEqual(dataset.headers[-1], "student_type")
                self.assertEqual(dataset[0][-1], value)

    def test_legacy_imports_normalize_and_preserve_missing_type(self):
        for resource_class in (StudentResource, AdminStudentResource):
            data = Dataset(headers=["registration_number", "email", "student_type"])
            data.append([self.student.pk, self.user.email, "day-scholar"])
            result = resource_class().import_data(data, raise_errors=True)
            self.assertFalse(result.has_errors())
            self.student.refresh_from_db()
            self.assertEqual(self.student.student_type, "DAY_SCHOLAR")
            self.assertFalse(self.student.hostel_id)
            self.assertFalse(self.student.room_number)
            data = Dataset(headers=["registration_number", "email"])
            data.append([self.student.pk, self.user.email])
            resource_class().import_data(data, raise_errors=True)
            self.student.refresh_from_db()
            self.assertEqual(self.student.student_type, "DAY_SCHOLAR")

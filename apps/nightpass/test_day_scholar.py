"""Day Scholar workflows and the existing Hosteller paths share the same services."""
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.global_settings.models import Settings
from apps.nightpass.admin import CampusResourceAdmin
from apps.nightpass.models import CampusResource, Hostel
from apps.nightpass.services.booking_policy import validate_booking_policy
from apps.nightpass.services.booking_service import create_pass_for_student
from apps.users.models import CustomUser, NightPass, Security, Student
from apps.users.services.deadline_evaluator import evaluate_active_pass_deadlines
from apps.users.services.pass_policy import (
    get_dashboard_status, get_scanner_status, required_location,
)
from apps.validation.services.scan_service import process_scan


def client_path(name, **kwargs):
    return reverse(name, **kwargs).removeprefix(settings.FORCE_SCRIPT_NAME or "")


class DayScholarWorkflowTests(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), time(20, 0)))
        self.hostel = Hostel.objects.create(name="A", contact_number="123", email="a@example.com")
        self.policy = Settings.objects.create(allow_sunday=True, scan_start_time=time(0),
            scan_end_time=time(23, 59), library_out_cutoff_time=time(23),
            enable_hostel_limits=True, enable_hostel_timers=True, last_out_from_hostel=time(19))
        self.resources = {}
        for pass_type in ("HOSTEL", "OUTSIDE", "DAY_SCHOLAR"):
            self.resources[pass_type] = CampusResource.objects.create(name=pass_type, description="Library",
                max_capacity=50, start_time=time(0), end_time=time(23, 59), is_display=True, is_booking=True,
                default_pass_type=pass_type, audience_type="DAY_SCHOLAR" if pass_type == "DAY_SCHOLAR" else "HOSTELLER")
        self.user = CustomUser.objects.create_user("day@example.com", None)
        self.student = Student.objects.create(user=self.user, name="Day Student", registration_number="DAY1",
            student_type="DAY_SCHOLAR", gender="male", year="1")
        self.hosteller_user = CustomUser.objects.create_user("hosteller@example.com", None)
        self.hosteller = Student.objects.create(user=self.hosteller_user, name="Hostel Student",
            registration_number="HOST1", hostel=self.hostel)
        self.scanners = {}
        for location in ("LIBRARY", "HOSTEL"):
            user = CustomUser.objects.create_user(location.lower()+"@example.com", None, user_type="security")
            Security.objects.filter(user=user).update(scanner_type=location, hostel=self.hostel if location == "HOSTEL" else None)
            user.refresh_from_db()
            self.scanners[location] = user

    def book(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            result = create_pass_for_student(self.user, self.resources["DAY_SCHOLAR"])
        self.assertTrue(result["status"], result)
        return NightPass.objects.get(user=self.user)

    def scan(self, location, at, student=None):
        with patch("django.utils.timezone.now", return_value=at):
            return process_scan((student or self.student).pk, self.scanners[location], now=at)

    def test_defaults_and_model_admin_configuration_validation(self):
        self.assertEqual(self.hosteller.student_type, "HOSTELLER")
        self.assertEqual(CampusResource().audience_type, "HOSTELLER")
        model_admin = CampusResourceAdmin(CampusResource, admin.site)
        self.assertIn("audience_type", model_admin.list_display)
        for audience, pass_type, valid in [("DAY_SCHOLAR", "DAY_SCHOLAR", True),
                ("HOSTELLER", "HOSTEL", True), ("HOSTELLER", "OUTSIDE", True),
                ("HOSTELLER", "DAY_SCHOLAR", False), ("DAY_SCHOLAR", "HOSTEL", False), ("DAY_SCHOLAR", "OUTSIDE", False)]:
            with self.subTest(audience=audience, pass_type=pass_type):
                resource = self.resources[pass_type]
                resource.audience_type = audience
                if valid:
                    resource.full_clean()
                else:
                    with self.assertRaises(ValidationError):
                        resource.full_clean()

    def test_home_visibility_and_null_hostel(self):
        for user, expected in [(self.user, {"DAY_SCHOLAR"}), (self.hosteller_user, {"HOSTEL", "OUTSIDE"})]:
            self.client.force_login(user)
            response = self.client.get(client_path("home"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.context["campus_resources"].values_list("name", flat=True)), expected)
            if user == self.user:
                self.assertContains(response, "Day Scholar")
                self.assertContains(response, "N/A")
                self.assertNotContains(response, "Inside Hostel")

    def test_direct_booking_urls_reject_both_audience_mismatches(self):
        for user, resource in [(self.user, "HOSTEL"), (self.user, "OUTSIDE"), (self.hosteller_user, "DAY_SCHOLAR")]:
            self.client.force_login(user)
            response = self.client.post(client_path("generate_pass", args=[resource]))
            import json
            self.assertEqual(json.loads(response.content)["reason_code"], "AUDIENCE_MISMATCH")
        self.assertFalse(NightPass.objects.exists())

    def test_day_scholar_books_despite_hostel_limits_and_last_out(self):
        # The explicit type also wins over an accidentally retained hostel assignment.
        self.student.hostel = self.hostel
        self.student.save(update_fields=["hostel"])
        user_pass = self.book()
        self.assertEqual(user_pass.current_step, 1)
        self.assertEqual(required_location(user_pass), "LIBRARY")
        self.assertEqual(get_dashboard_status(user_pass), "Booked")
        self.assertFalse(user_pass.is_late_in_transit())

    def test_library_flow_cooldown_wrong_scanner_and_completion(self):
        user_pass = self.book()
        self.assertEqual(self.scan("HOSTEL", self.now)["reason_code"], "WRONG_SCANNER_LOCATION")
        first = self.scan("LIBRARY", self.now)
        self.assertTrue(first["status"])
        self.assertEqual(first["user"]["hostel"], "N/A")
        user_pass.refresh_from_db()
        self.assertEqual(user_pass.current_step, 2)
        self.assertIsNotNone(user_pass.library_in_time)
        self.assertEqual(self.scan("LIBRARY", self.now + timedelta(minutes=4, seconds=59))["reason_code"], "RECENT_SCAN_BLOCKED")
        self.assertTrue(self.scan("LIBRARY", self.now + timedelta(minutes=5))["status"])
        user_pass.refresh_from_db(); self.student.refresh_from_db()
        self.assertEqual(user_pass.current_step, 4)
        self.assertFalse(user_pass.valid)
        self.assertFalse(self.student.has_booked)
        self.assertIsNotNone(user_pass.library_out_time)
        self.assertIsNone(user_pass.hostel_checkout_time)
        self.assertIsNone(user_pass.hostel_checkin_time)
        self.assertIsNone(self.student.hostel_checkin_time)
        self.assertIsNone(required_location(user_pass))
        self.assertEqual(get_scanner_status(user_pass), "Completed")
        self.assertEqual(self.scan("HOSTEL", self.now + timedelta(minutes=10))["reason_code"], "NO_ACTIVE_PASS")

    def test_day_scholar_general_booking_rules_still_apply(self):
        cases = [("max_violation_count", 0, "BLOCKED_MAX_VIOLATIONS"),
                 ("allow_"+self.now.strftime("%A").lower(), False, "NIGHT_PASS_NOT_AVAILABLE_ON_SUNDAYS"),
                 ("enable_gender_ratio", True, "GENDER_QUOTA_FULL"),
                 ("enable_yearwise_limits", True, "YEAR_QUOTA_FULL")]
        self.policy.male_ratio = 0
        self.policy.save()
        for field, value, code in cases:
            original = getattr(self.policy, field)
            setattr(self.policy, field, value); self.policy.save()
            with patch("django.utils.timezone.now", return_value=self.now):
                self.assertEqual(validate_booking_policy(self.student, self.resources["DAY_SCHOLAR"])["reason_code"], code)
            setattr(self.policy, field, original); self.policy.save()
        resource = self.resources["DAY_SCHOLAR"]
        resource.slots_booked = resource.max_capacity; resource.save()
        with patch("django.utils.timezone.now", return_value=self.now):
            self.assertEqual(validate_booking_policy(self.student, resource)["reason_code"], "CAPACITY_FULL")
        resource.slots_booked = 0; resource.end_time = time(19); resource.save()
        with patch("django.utils.timezone.now", return_value=self.now):
            self.assertEqual(validate_booking_policy(self.student, resource)["reason_code"], "OUTSIDE_BOOKING_WINDOW")

    def test_library_in_has_no_transit_deadline_and_late_library_out_is_recorded(self):
        user_pass = self.book()
        NightPass.objects.filter(pk=user_pass.pk).update(hostel_checkout_time=self.now-timedelta(hours=4))
        self.assertTrue(self.scan("LIBRARY", self.now)["status"])
        self.student.refresh_from_db()
        self.assertEqual(self.student.violation_flags, 0)
        result = self.scan("LIBRARY", self.now.replace(hour=23, minute=10))
        self.assertTrue(result["violation_occurred"])
        user_pass.refresh_from_db(); self.student.refresh_from_db()
        self.assertEqual(user_pass.violation_code, "LATE_LIBRARY_OUT")
        self.assertEqual(self.student.violation_flags, 1)
        self.assertFalse(user_pass.valid)
        self.assertEqual(user_pass.current_step, 4)

    def test_stale_passes_never_require_hostel_return(self):
        for step in (1, 2, 3):
            with self.subTest(step=step):
                user_pass = self.book()
                NightPass.objects.filter(pk=user_pass.pk).update(date=timezone.localdate()-timedelta(days=1), current_step=step,
                    library_out_time=self.now-timedelta(days=1))
                with patch("django.utils.timezone.now", return_value=self.now):
                    summary = evaluate_active_pass_deadlines(now=self.now)
                self.assertEqual(summary["missed_hostel_in"], 0)
                user_pass.refresh_from_db(); self.student.refresh_from_db()
                self.assertFalse(user_pass.valid)
                self.assertFalse(self.student.has_booked)
                self.assertIsNone(self.student.hostel_checkin_time)
                self.assertNotIn("LATE_HOSTEL_IN", user_pass.violation_code or "")
                self.assertEqual(user_pass.violation_code or "", "LATE_LIBRARY_OUT" if step == 2 else "")
                user_pass.delete()

    def test_hostel_and_outside_scan_sequences_are_unchanged(self):
        for pass_type, locations in [("HOSTEL", ["HOSTEL", "LIBRARY", "LIBRARY", "HOSTEL"]),
                                     ("OUTSIDE", ["LIBRARY", "LIBRARY", "HOSTEL"])]:
            with self.subTest(pass_type=pass_type):
                Student.objects.filter(pk=self.hosteller.pk).update(last_scan_at=None, has_booked=True)
                user_pass = NightPass.objects.create(user=self.hosteller_user, campus_resource=self.resources[pass_type],
                    start_time=time(20), end_time=self.now+timedelta(hours=4))
                self.assertEqual(user_pass.current_step, 0 if pass_type == "HOSTEL" else 1)
                for index, location in enumerate(locations):
                    result = self.scan(location, self.now+timedelta(minutes=index*5), self.hosteller)
                    self.assertTrue(result["status"], result)
                    user_pass.refresh_from_db()
                    self.assertEqual(user_pass.current_step, (0 if pass_type == "HOSTEL" else 1)+index+1)
                self.hosteller.refresh_from_db()
                self.assertFalse(user_pass.valid)
                self.assertFalse(self.hosteller.has_booked)
                self.assertIsNotNone(user_pass.hostel_checkin_time)

    def test_scanner_dashboard_and_combined_admin_exports(self):
        self.book()
        for location in ("LIBRARY", "HOSTEL"):
            self.client.force_login(self.scanners[location])
            response = self.client.get(client_path("scanner"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context["student_passes"]), 1 if location == "LIBRARY" else 0)
        admin_user = CustomUser.objects.create_user("admin@example.com", None, user_type="admin")
        self.client.force_login(admin_user)
        NightPass.objects.create(user=self.hosteller_user, campus_resource=self.resources["HOSTEL"],
            start_time=time(20), end_time=self.now+timedelta(hours=4))
        response = self.client.get(client_path("admin_dashboard"))
        self.assertEqual(response.context["active_passes"], 2)
        self.assertEqual(response.context["in_transit"], 0)
        self.assertContains(response, "Day Scholar")
        self.assertContains(response, "N/A")
        for url, params in [("download_report_range", {"start_date": timezone.localdate().isoformat(), "end_date": timezone.localdate().isoformat()}),
                            ("download_admin_table_excel", {"scope": "activity"})]:
            response = self.client.get(client_path(url), params)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b"PK"))

    def test_day_scholar_scan_window_and_home_have_no_transit_timer(self):
        self.book()
        self.policy.scan_start_time = time(20)
        self.policy.scan_end_time = time(22, 30)
        self.policy.save()
        result = self.scan("LIBRARY", self.now.replace(hour=19))
        self.assertEqual(result["reason_code"], "SCAN_WINDOW_CLOSED")
        self.client.force_login(self.user)
        response = self.client.get(client_path("home"))
        self.assertNotContains(response, 'id="transit-timer"')
        self.assertContains(response, "Proceed to Library IN")

    def test_invalid_resource_configuration_cannot_book(self):
        resource = self.resources["DAY_SCHOLAR"]
        resource.default_pass_type = "HOSTEL"
        resource.save()
        result = create_pass_for_student(self.user, resource)
        self.assertEqual(result["reason_code"], "INVALID_RESOURCE_CONFIGURATION")
        self.assertFalse(NightPass.objects.exists())

    def test_null_hostel_all_student_export_and_detail_scopes(self):
        from io import BytesIO
        from openpyxl import load_workbook
        self.book()
        admin_user = CustomUser.objects.create_user("export-admin@example.com", None, user_type="admin")
        self.client.force_login(admin_user)
        for params in [{"scope": "students_all"}, {"scope": "student_search", "q": self.student.pk},
                       {"scope": "student_activity", "student": self.student.pk},
                       {"scope": "detail", "segment": "active-passes"}]:
            with self.subTest(params=params):
                response = self.client.get(client_path("download_admin_table_excel"), params)
                self.assertEqual(response.status_code, 200)
                workbook = load_workbook(BytesIO(response.content))
                rows = list(workbook.active.values)
                self.assertTrue(any("N/A" in row for row in rows))
                self.assertFalse(any("Inside Hostel" in row for row in rows if self.student.name in row))
        response = self.client.get(client_path("simple_student_list"))
        self.assertContains(response, "N/A")
        self.assertContains(response, "Proceed to Library IN")
        self.assertContains(response, "Inside Hostel", count=1)

    def test_invalid_day_scholar_hostel_steps_cannot_be_scanned(self):
        user_pass = self.book()
        for step in (0, 3):
            NightPass.objects.filter(pk=user_pass.pk).update(current_step=step)
            for location in ("LIBRARY", "HOSTEL"):
                self.assertEqual(self.scan(location, self.now)["reason_code"], "INVALID_PASS_STATE")


    def test_resource_admin_form_and_import_reject_audience_mismatch(self):
        from django.test import RequestFactory
        from tablib import Dataset
        from apps.nightpass.admin import CampusResourceResource
        admin_user = CustomUser.objects.create_user("config-admin@example.com", None, user_type="admin")
        request = RequestFactory().get("/admin/")
        request.user = admin_user
        model_admin = CampusResourceAdmin(CampusResource, admin.site)
        form_class = model_admin.get_form(request)
        form = form_class(data={"name": "Invalid", "description": "Library", "max_capacity": 10,
            "start_time": "00:00", "end_time": "23:59", "audience_type": "DAY_SCHOLAR", "default_pass_type": "HOSTEL"})
        self.assertFalse(form.is_valid())
        self.assertIn("default_pass_type", form.errors)
        resource = self.resources["DAY_SCHOLAR"]
        data = Dataset(headers=["id", "audience_type", "default_pass_type"])
        data.append([resource.pk, "DAY_SCHOLAR", "HOSTEL"])
        result = CampusResourceResource().import_data(data)
        self.assertTrue(result.has_validation_errors())
        resource.refresh_from_db()
        self.assertEqual(resource.default_pass_type, "DAY_SCHOLAR")

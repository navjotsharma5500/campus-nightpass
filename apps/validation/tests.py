from datetime import datetime, time, timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.global_settings.models import Settings
from apps.nightpass.models import CampusResource, Hostel
from apps.nightpass.services.booking_policy import validate_booking_policy
from apps.users.models import CustomUser, NightPass, Student
from apps.users.services.deadline_evaluator import evaluate_active_pass_deadlines
from .services.lifecycle import (
    step_label,
    transition_checkin_to_hostel,
    transition_checkin_to_library,
    transition_checkout_from_library,
)


class LifecycleServiceTests(SimpleTestCase):
    def test_step_label_map(self):
        self.assertEqual(step_label(0), "Hostel Out")
        self.assertEqual(step_label(1), "Library In")
        self.assertEqual(step_label(2), "Library Out")
        self.assertEqual(step_label(3), "Hostel In")

    def test_unknown_step_label(self):
        self.assertEqual(step_label(99), "Valid Scan")


class AdminDashboardEnhancementTests(TestCase):
    def setUp(self):
        self.admin_user = CustomUser.objects.create_user(
            email="admin@example.com",
            password="pass12345",
            user_type="admin",
            first_name="Admin",
        )
        self.client.force_login(self.admin_user)

        self.hostel = Hostel.objects.create(
            name="Test Hostel",
            contact_number="9999999999",
            email="hostel@example.com",
        )
        now = timezone.now()
        self.resource = CampusResource.objects.create(
            name="Main Library",
            description="Library",
            max_capacity=50,
            start_time=now.time(),
            end_time=(now + timedelta(hours=2)).time(),
            default_pass_type="HOSTEL",
        )

        self.student_user_today = CustomUser.objects.create_user(
            email="today@student.com",
            password="pass12345",
            user_type="student",
        )
        self.student_today = Student.objects.create(
            user=self.student_user_today,
            name="Today Student",
            registration_number="REGTODAY01",
            hostel=self.hostel,
        )

        self.student_user_old = CustomUser.objects.create_user(
            email="old@student.com",
            password="pass12345",
            user_type="student",
        )
        self.student_old = Student.objects.create(
            user=self.student_user_old,
            name="Old Student",
            registration_number="REGOLD01",
            hostel=self.hostel,
        )

    def _create_pass(self, user, pass_date):
        now = timezone.now()
        user_pass = NightPass.objects.create(
            user=user,
            start_time=now.time(),
            end_time=now + timedelta(hours=3),
            campus_resource=self.resource,
        )
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(date=pass_date)
        user_pass.refresh_from_db()
        return user_pass

    def test_dashboard_date_filter_limits_records_to_selected_day(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self._create_pass(self.student_user_today, today)
        self._create_pass(self.student_user_old, yesterday)

        response = self.client.get(reverse("admin_dashboard"), {"date": today.isoformat()})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Today Student")
        self.assertNotContains(response, "Old Student")

    def test_activity_excel_download_endpoint_returns_xlsx(self):
        today = timezone.localdate()
        self._create_pass(self.student_user_today, today)

        response = self.client.get(
            reverse("download_admin_table_excel"),
            {"scope": "activity", "date": today.isoformat(), "activity": "all"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            response["Content-Type"],
        )
        self.assertIn("attachment; filename=", response["Content-Disposition"])

    def test_global_search_shows_student_timeline(self):
        today = timezone.localdate()
        self._create_pass(self.student_user_today, today)

        response = self.client.get(
            reverse("admin_dashboard"),
            {"date": today.isoformat(), "q": "REGTODAY01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "STUDENT ACTIVITY TIMELINE")
        self.assertContains(response, "Today Student")

    def test_blocked_students_count_not_limited_by_selected_date(self):
        Settings.objects.create(max_violation_count=1)
        self.student_old.violation_flags = 2
        self.student_old.save(update_fields=["violation_flags"])

        today = timezone.localdate()
        self._create_pass(self.student_user_today, today)
        old_day = today - timedelta(days=2)
        self._create_pass(self.student_user_old, old_day)

        response = self.client.get(reverse("admin_dashboard"), {"date": today.isoformat()})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["blocked_students"], 1)

    def test_blocked_students_count_includes_defaulters(self):
        Settings.objects.create(max_violation_count=50)
        today = timezone.localdate()
        old_day = today - timedelta(days=2)
        user_pass = self._create_pass(self.student_user_old, old_day)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            defaulter=True,
            violation_code="MISSED_LIBRARY_IN",
            defaulter_remarks="Missed scan",
        )

        response = self.client.get(reverse("admin_dashboard"), {"date": today.isoformat()})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["blocked_students"], 1)


class ViolationThresholdBehaviorTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.hostel = Hostel.objects.create(
            name="Threshold Hostel",
            contact_number="9999999999",
            email="threshold@hostel.com",
        )
        self.resource = CampusResource.objects.create(
            name="Threshold Library",
            description="Library",
            max_capacity=100,
            start_time=time(0, 0),
            end_time=time(23, 59),
            is_booking=True,
            is_display=True,
            default_pass_type="HOSTEL",
        )
        self.settings = Settings.objects.create(
            max_violation_count=3,
            allow_monday=True,
            allow_tuesday=True,
            allow_wednesday=True,
            allow_thursday=True,
            allow_friday=True,
            allow_saturday=True,
            allow_sunday=True,
            frontend_checkin_timer=40,
            backend_checkin_timer=40,
            library_timer_for_hostel_out=40,
            library_out_cutoff_time=time(23, 0),
        )
        self.user = CustomUser.objects.create_user(
            email="threshold@student.com",
            password="pass12345",
            user_type="student",
        )
        self.student = Student.objects.create(
            user=self.user,
            name="Threshold Student",
            registration_number="REGTHR01",
            hostel=self.hostel,
        )

    def _create_pass(self, current_step=1):
        now = timezone.now()
        user_pass = NightPass.objects.create(
            user=self.user,
            start_time=now.time(),
            end_time=now + timedelta(hours=4),
            campus_resource=self.resource,
        )
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            date=timezone.localdate(),
            current_step=current_step,
            valid=True,
        )
        user_pass.refresh_from_db()
        return user_pass

    def test_booking_block_uses_configured_max_violation_count(self):
        self.student.violation_flags = 1
        self.student.save(update_fields=["violation_flags"])
        self.assertIsNone(validate_booking_policy(self.student, self.resource))

        self.student.violation_flags = 3
        self.student.save(update_fields=["violation_flags"])
        blocked = validate_booking_policy(self.student, self.resource)
        self.assertEqual(blocked["reason_code"], "BLOCKED_MAX_VIOLATIONS")

        self.settings.max_violation_count = 10
        self.settings.save(update_fields=["max_violation_count"])
        self.assertIsNone(validate_booking_policy(self.student, self.resource))

    def test_multiple_violations_same_pass_increment_once(self):
        user_pass = self._create_pass(current_step=1)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            hostel_checkout_time=timezone.now() - timedelta(minutes=50)
        )
        user_pass.refresh_from_db()

        transition_checkin_to_library(user_pass)
        self.student.refresh_from_db()
        self.assertEqual(self.student.violation_flags, 1)

        transition_checkout_from_library(user_pass)
        user_pass.refresh_from_db()
        user_pass.library_out_time = timezone.now() - timedelta(minutes=50)
        user_pass.save(update_fields=["library_out_time"])

        transition_checkin_to_hostel(self.student)
        self.student.refresh_from_db()
        self.assertEqual(self.student.violation_flags, 1)

    def test_library_in_status_after_11_pm_marks_violation(self):
        user_pass = self._create_pass(current_step=2)
        now = timezone.now()
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            date=timezone.localdate(now),
            library_in_time=now - timedelta(hours=1),
            library_out_time=None,
            valid=True,
            defaulter=False,
            violation_code=None,
            defaulter_remarks="",
        )
        user_pass.refresh_from_db()

        cutoff_now = timezone.make_aware(
            datetime.combine(timezone.localdate(now), time(23, 5)),
            timezone.get_current_timezone(),
        )
        evaluate_active_pass_deadlines(now=cutoff_now)
        self.student.refresh_from_db()
        user_pass.refresh_from_db()

        self.assertTrue(user_pass.defaulter)
        self.assertIn("MISSED_LIBRARY_OUT", user_pass.violation_code or "")
        self.assertEqual(self.student.violation_flags, 1)

        evaluate_active_pass_deadlines(now=cutoff_now + timedelta(minutes=1))
        self.student.refresh_from_db()
        self.assertEqual(self.student.violation_flags, 1)

    def test_library_out_cutoff_time_respects_settings_value(self):
        self.settings.library_out_cutoff_time = time(22, 0)
        self.settings.save(update_fields=["library_out_cutoff_time"])

        user_pass = self._create_pass(current_step=2)
        now = timezone.now()
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            date=timezone.localdate(now),
            library_in_time=now - timedelta(hours=1),
            library_out_time=None,
            valid=True,
            defaulter=False,
            violation_code=None,
            defaulter_remarks="",
        )
        user_pass.refresh_from_db()

        before_cutoff = timezone.make_aware(
            datetime.combine(timezone.localdate(now), time(21, 59)),
            timezone.get_current_timezone(),
        )
        evaluate_active_pass_deadlines(now=before_cutoff)
        user_pass.refresh_from_db()
        self.assertFalse(user_pass.defaulter)

        after_cutoff = timezone.make_aware(
            datetime.combine(timezone.localdate(now), time(22, 5)),
            timezone.get_current_timezone(),
        )
        evaluate_active_pass_deadlines(now=after_cutoff)
        self.student.refresh_from_db()
        user_pass.refresh_from_db()
        self.assertTrue(user_pass.defaulter)
        self.assertEqual(self.student.violation_flags, 1)

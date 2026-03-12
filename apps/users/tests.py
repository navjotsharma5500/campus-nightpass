from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.global_settings.models import Settings
from apps.nightpass.models import CampusResource, Hostel
from apps.nightpass.services.booking_service import create_pass_for_student
from apps.users.models import CustomUser, NightPass, Security, Student
from apps.users.services.deadline_evaluator import evaluate_active_pass_deadlines
from apps.users.services.pass_policy import get_active_pass_for_user
from apps.validation.services.lifecycle import (
    transition_checkin_to_hostel,
    transition_checkin_to_library,
    transition_checkout_from_library,
)
from apps.validation.services.scan_service import process_scan


class UnifiedNightPassPolicyTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.hostel = Hostel.objects.create(
            name="A Hostel",
            contact_number="9999999999",
            email="hostel@example.com",
            frontend_checkin_timer=30,
            backend_checkin_timer=30,
        )
        self.hostel_resource = CampusResource.objects.create(
            name="Central Library",
            description="Library",
            max_capacity=100,
            start_time=time(0, 0),
            end_time=time(23, 59),
            is_booking=True,
            is_display=True,
            default_pass_type="HOSTEL",
        )
        self.outside_resource = CampusResource.objects.create(
            name="Outside Library",
            description="Library",
            max_capacity=100,
            start_time=time(0, 0),
            end_time=time(23, 59),
            is_booking=True,
            is_display=True,
            default_pass_type="OUTSIDE",
        )
        self.settings = Settings.objects.create(
            max_violation_count=5,
            allow_monday=True,
            allow_tuesday=True,
            allow_wednesday=True,
            allow_thursday=True,
            allow_friday=True,
            allow_saturday=True,
            allow_sunday=True,
            frontend_checkin_timer=30,
            backend_checkin_timer=30,
            library_timer_for_hostel_out=30,
            library_out_cutoff_time=time(23, 0),
            scan_start_time=time(0, 0),
            scan_end_time=time(23, 59),
            slot_cancel_timer=time(20, 0),
        )

        self.student_user = CustomUser.objects.create_user(
            email="student@example.com",
            password="pass12345",
            user_type="student",
        )
        self.student = Student.objects.create(
            user=self.student_user,
            name="Night Pass Student",
            registration_number="REG123456",
            hostel=self.hostel,
        )

        self.library_scanner_user = CustomUser.objects.create_user(
            email="library-scanner@example.com",
            password="pass12345",
            user_type="security",
        )
        Security.objects.filter(user=self.library_scanner_user).update(
            name="Library Scanner",
            scanner_type=Security.SCANNER_LIBRARY,
            hostel=None,
        )

        self.hostel_scanner_user = CustomUser.objects.create_user(
            email="hostel-scanner@example.com",
            password="pass12345",
            user_type="security",
        )
        Security.objects.filter(user=self.hostel_scanner_user).update(
            name="Hostel Scanner",
            scanner_type=Security.SCANNER_HOSTEL,
            hostel=self.hostel,
        )

    def _create_pass(self, resource=None, pass_date=None, current_step=None, valid=True):
        resource = resource or self.hostel_resource
        pass_date = pass_date or timezone.localdate(self.now)
        user_pass = NightPass.objects.create(
            user=self.student_user,
            start_time=self.now.time(),
            end_time=self.now + timedelta(hours=4),
            campus_resource=resource,
            valid=valid,
        )
        updates = {"date": pass_date, "valid": valid}
        if current_step is not None:
            updates["current_step"] = current_step
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(**updates)
        user_pass.refresh_from_db()
        return user_pass

    def test_previous_day_pass_expires_and_new_booking_is_allowed(self):
        yesterday = timezone.localdate(self.now) - timedelta(days=1)
        old_pass = self._create_pass(pass_date=yesterday, current_step=3)
        NightPass.objects.filter(pass_id=old_pass.pass_id).update(
            library_out_time=self.now - timedelta(days=1, minutes=40),
            valid=True,
        )
        self.student.has_booked = True
        self.student.is_checked_in = False
        self.student.save(update_fields=["has_booked", "is_checked_in"])

        result = create_pass_for_student(self.student_user, self.hostel_resource)

        old_pass.refresh_from_db()
        self.student.refresh_from_db()
        self.assertTrue(result["status"])
        self.assertFalse(old_pass.valid)
        self.assertIn("LATE_HOSTEL_IN", old_pass.violation_code or "")
        self.assertEqual(self.student.violation_flags, 1)
        self.assertTrue(self.student.has_booked)
        self.assertTrue(self.student.is_checked_in)
        self.assertEqual(NightPass.objects.filter(user=self.student_user, valid=True).count(), 1)

    def test_multiple_booking_is_blocked_until_existing_booking_is_cancelled(self):
        first = create_pass_for_student(self.student_user, self.hostel_resource)
        blocked = create_pass_for_student(self.student_user, self.hostel_resource)

        self.assertTrue(first["status"])
        self.assertFalse(blocked["status"])
        self.assertEqual(blocked["reason_code"], "ACTIVE_PASS_EXISTS")
        self.assertEqual(NightPass.objects.filter(user=self.student_user, valid=True).count(), 1)

    def test_outside_hostel_pass_scans_at_library_without_hostel_out(self):
        user_pass = self._create_pass(resource=self.outside_resource, current_step=1)

        response = process_scan(self.student.registration_number, self.library_scanner_user, now=self.now)

        user_pass.refresh_from_db()
        self.assertTrue(response["status"])
        self.assertEqual(user_pass.current_step, 2)
        self.assertIsNotNone(user_pass.library_in_time)

    def test_each_late_scan_adds_one_violation_on_same_pass(self):
        user_pass = self._create_pass(current_step=1)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            hostel_checkout_time=self.now - timedelta(minutes=45)
        )
        user_pass.refresh_from_db()

        transition_checkin_to_library(user_pass)
        transition_checkout_from_library(user_pass)
        user_pass.refresh_from_db()
        user_pass.library_out_time = timezone.now() - timedelta(minutes=45)
        user_pass.save(update_fields=["library_out_time"])

        transition_checkin_to_hostel(self.student)
        self.student.refresh_from_db()
        user_pass.refresh_from_db()

        self.assertEqual(self.student.violation_flags, 2)
        self.assertIn("LATE_LIBRARY_IN", user_pass.violation_code or "")
        self.assertIn("LATE_HOSTEL_IN", user_pass.violation_code or "")

    def test_deadline_and_late_scan_do_not_double_count_same_step(self):
        user_pass = self._create_pass(current_step=1)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            hostel_checkout_time=self.now - timedelta(minutes=45)
        )
        user_pass.refresh_from_db()

        evaluate_active_pass_deadlines(now=self.now)
        self.student.refresh_from_db()
        self.assertEqual(self.student.violation_flags, 1)

        response = process_scan(self.student.registration_number, self.library_scanner_user, now=self.now)

        self.student.refresh_from_db()
        user_pass.refresh_from_db()
        self.assertTrue(response["status"])
        self.assertTrue(response.get("violation_occurred"))
        self.assertIn("Violation Recorded", response.get("violation_message", ""))
        self.assertEqual(self.student.violation_flags, 1)
        self.assertEqual((user_pass.violation_code or "").count("LATE_LIBRARY_IN"), 1)


    def test_second_scan_within_five_minutes_is_blocked_across_locations(self):
        user_pass = self._create_pass(current_step=0)

        first_response = process_scan(self.student.registration_number, self.hostel_scanner_user, now=self.now)
        blocked_response = process_scan(
            self.student.registration_number,
            self.library_scanner_user,
            now=self.now + timedelta(minutes=1),
        )

        user_pass.refresh_from_db()
        self.student.refresh_from_db()
        self.assertTrue(first_response["status"])
        self.assertFalse(blocked_response["status"])
        self.assertEqual(blocked_response["reason_code"], "RECENT_SCAN_BLOCKED")
        self.assertEqual(
            blocked_response["message"],
            "Scan already recorded. Please wait 5 minutes before scanning again.",
        )
        self.assertEqual(self.student.last_scan_at, self.now)

    def test_deadline_evaluator_triggers_immediately_after_30_minute_deadline(self):
        user_pass = self._create_pass(current_step=1)
        checkout_time = self.now - timedelta(minutes=29, seconds=59)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(hostel_checkout_time=checkout_time)
        user_pass.refresh_from_db()

        evaluate_active_pass_deadlines(now=self.now)
        user_pass.refresh_from_db()
        self.assertFalse(user_pass.defaulter)

        evaluate_active_pass_deadlines(now=checkout_time + timedelta(minutes=30, seconds=1))
        self.student.refresh_from_db()
        user_pass.refresh_from_db()
        self.assertTrue(user_pass.defaulter)
        self.assertIn("LATE_LIBRARY_IN", user_pass.violation_code or "")
        self.assertEqual(self.student.violation_flags, 1)

    def test_cancel_respects_configured_slot_cancel_timer(self):
        create_pass_for_student(self.student_user, self.hostel_resource)
        self.client.force_login(self.student_user)
        blocked_now = timezone.make_aware(
            datetime.combine(timezone.localdate(), time(20, 1)),
            timezone.get_current_timezone(),
        )

        with patch("apps.nightpass.views.timezone.now", return_value=blocked_now):
            response = self.client.get(reverse("cancel_pass"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Cannot cancel pass after 8:00 PM", response.content.decode())
        self.assertEqual(NightPass.objects.filter(user=self.student_user, valid=True).count(), 1)

    def test_get_active_pass_for_user_hides_stale_pass(self):
        yesterday = timezone.localdate(self.now) - timedelta(days=1)
        self._create_pass(pass_date=yesterday, current_step=2)

        active_pass = get_active_pass_for_user(self.student_user, now=self.now)

        self.assertIsNone(active_pass)
        self.assertEqual(NightPass.objects.filter(user=self.student_user, valid=True).count(), 0)




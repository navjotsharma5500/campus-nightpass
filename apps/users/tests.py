from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.global_settings.models import Settings
from apps.nightpass.models import CampusResource, Hostel
from apps.validation.services.scan_service import process_scan
from .models import CustomUser, NightPass, Security, Student
from .services.deadline_evaluator import evaluate_active_pass_deadlines


class LateScanViolationFlowTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.hostel = Hostel.objects.create(
            name="A Hostel",
            contact_number="9999999999",
            email="hostel@example.com",
            frontend_checkin_timer=5,
            backend_checkin_timer=5,
        )
        self.resource = CampusResource.objects.create(
            name="Library",
            description="Central Library",
            max_capacity=100,
            start_time=self.now.time(),
            end_time=(self.now + timedelta(hours=2)).time(),
            default_pass_type="HOSTEL",
        )
        Settings.objects.create(
            frontend_checkin_timer=5,
            backend_checkin_timer=5,
            library_timer_for_hostel_out=5,
            scan_start_time=(self.now - timedelta(hours=1)).time(),
            scan_end_time=(self.now + timedelta(hours=1)).time(),
        )

        self.student_user = CustomUser.objects.create_user(
            email="student@example.com",
            password="pass12345",
            user_type="student",
            first_name="Late",
            last_name="Student",
        )
        self.student = Student.objects.create(
            user=self.student_user,
            name="Late Student",
            registration_number="REG123456",
            hostel=self.hostel,
        )

        self.scanner_user = CustomUser.objects.create_user(
            email="scanner@example.com",
            password="pass12345",
            user_type="security",
            first_name="Scanner",
        )
        Security.objects.filter(user=self.scanner_user).update(
            name="Scanner User",
            scanner_type=Security.SCANNER_LIBRARY,
            hostel=None,
        )

    def test_deadline_evaluator_keeps_pass_valid_for_late_library_in_scan(self):
        user_pass = NightPass.objects.create(
            user=self.student_user,
            start_time=self.now.time(),
            end_time=self.now + timedelta(hours=4),
            campus_resource=self.resource,
        )
        user_pass.current_step = 1
        user_pass.hostel_checkout_time = self.now - timedelta(minutes=20)
        user_pass.save(update_fields=["current_step", "hostel_checkout_time"])

        summary = evaluate_active_pass_deadlines(now=self.now)
        user_pass.refresh_from_db()
        self.student.refresh_from_db()

        self.assertEqual(summary["missed_library_in"], 1)
        self.assertEqual(summary["expired_passes"], 0)
        self.assertTrue(user_pass.valid)
        self.assertTrue(user_pass.defaulter)
        self.assertIn("MISSED_LIBRARY_IN", user_pass.violation_code)
        self.assertEqual(self.student.violation_flags, 1)

        response = process_scan(self.student.registration_number, self.scanner_user, now=self.now)
        user_pass.refresh_from_db()

        self.assertTrue(response["status"])
        self.assertEqual(response["reason_code"], "TRANSITION_APPLIED")
        self.assertTrue(response.get("violation_occurred"))
        self.assertEqual(response.get("violation_message"), "Violation Occurred for Late Scan")
        self.assertEqual(user_pass.current_step, 2)
        self.assertIn("LATE_LIBRARY_IN", user_pass.violation_code)

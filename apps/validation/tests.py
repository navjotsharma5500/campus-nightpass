from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.nightpass.models import CampusResource, Hostel
from apps.users.models import CustomUser, NightPass, Student
from .services.lifecycle import step_label


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

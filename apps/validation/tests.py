from datetime import time, timedelta

from django.test import SimpleTestCase, TestCase
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.global_settings.models import Settings
from apps.nightpass.models import CampusResource, Hostel
from apps.users.models import CustomUser, NightPass, Student
from apps.users.services.pass_policy import get_dashboard_status, get_scanner_status, step_label


def client_path(*args, **kwargs):
    """Test Client takes PATH_INFO without the deployment script prefix."""
    return reverse(*args, **kwargs).removeprefix(settings.FORCE_SCRIPT_NAME or "")


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
        self.resource = CampusResource.objects.create(
            name="Main Library",
            description="Library",
            max_capacity=50,
            start_time=time(0, 0),
            end_time=time(23, 59),
            default_pass_type="HOSTEL",
        )
        Settings.objects.create(max_violation_count=3)

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

    def _create_pass(self, user, pass_date, current_step=1, valid=True):
        now = timezone.now()
        user_pass = NightPass.objects.create(
            user=user,
            start_time=now.time(),
            end_time=now + timedelta(hours=3),
            campus_resource=self.resource,
        )
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(date=pass_date, current_step=current_step, valid=valid)
        user_pass.refresh_from_db()
        return user_pass

    def test_dashboard_date_filter_limits_records_to_selected_day(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self._create_pass(self.student_user_today, today)
        self._create_pass(self.student_user_old, yesterday)

        response = self.client.get(client_path("admin_dashboard"), {"date": today.isoformat()})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Today Student")
        self.assertNotContains(response, "Old Student")

    def test_dashboard_status_keeps_violation_visible_after_expiry(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        user_pass = self._create_pass(self.student_user_old, yesterday, current_step=3, valid=False)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            defaulter=True,
            violation_code="LATE_HOSTEL_IN",
            defaulter_remarks="Late Hostel IN",
        )
        user_pass.refresh_from_db()

        status = get_dashboard_status(user_pass, max_violations=3)

        self.assertEqual(status, "Violation")

    def test_scanner_status_uses_unified_policy(self):
        today = timezone.localdate()
        user_pass = self._create_pass(self.student_user_today, today, current_step=2, valid=True)

        status = get_scanner_status(user_pass)

        self.assertEqual(status, "Library IN")

    def test_download_report_range_handles_timezone_aware_datetimes(self):
        today = timezone.localdate()
        now = timezone.now()
        user_pass = self._create_pass(self.student_user_today, today, current_step=4, valid=False)
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            hostel_checkout_time=now,
            library_in_time=now + timedelta(minutes=10),
            library_out_time=now + timedelta(hours=1),
            hostel_checkin_time=now + timedelta(hours=1, minutes=20),
        )

        response = self.client.get(
            client_path("download_report_range"),
            {"start_date": today.isoformat(), "end_date": today.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertGreater(len(response.content), 0)

    def test_dashboard_exposes_all_six_kpi_metrics(self):
        today = timezone.localdate()
        self._create_pass(self.student_user_today, today, current_step=2, valid=True)

        response = self.client.get(client_path("admin_dashboard"), {"date": today.isoformat()})

        self.assertEqual(response.status_code, 200)
        for key in (
            "active_checkins",
            "active_passes",
            "violation_count",
            "in_transit",
            "completed_today",
            "blocked_students",
        ):
            self.assertIn(key, response.context)

    def test_dashboard_activity_filter_query_param_still_works(self):
        today = timezone.localdate()
        self._create_pass(self.student_user_today, today, current_step=2, valid=True)

        response = self.client.get(
            client_path("admin_dashboard"),
            {"date": today.isoformat(), "activity": "in_library"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["activity_tab"], "in_library")


class AnalyticsPageEnhancementTests(TestCase):
    def setUp(self):
        self.admin_user = CustomUser.objects.create_user(
            email="analytics-admin@example.com",
            password="pass12345",
            user_type="admin",
            first_name="Admin",
        )
        self.client.force_login(self.admin_user)

        self.hostel = Hostel.objects.create(
            name="Analytics Hostel",
            contact_number="9999999998",
            email="analytics-hostel@example.com",
        )
        self.hostel_resource = CampusResource.objects.create(
            name="Hostel Library",
            description="Library",
            max_capacity=50,
            start_time=time(0, 0),
            end_time=time(23, 59),
            default_pass_type="HOSTEL",
        )
        self.outside_resource = CampusResource.objects.create(
            name="Outside Library",
            description="Library",
            max_capacity=50,
            start_time=time(0, 0),
            end_time=time(23, 59),
            default_pass_type="OUTSIDE",
        )
        self.day_scholar_resource = CampusResource.objects.create(
            name="Day Scholar Library",
            description="Library",
            max_capacity=50,
            start_time=time(0, 0),
            end_time=time(23, 59),
            default_pass_type="DAY_SCHOLAR",
        )

        self.hosteller_user = CustomUser.objects.create_user(
            email="hosteller@example.com",
            password="pass12345",
            user_type="student",
        )
        self.hosteller = Student.objects.create(
            user=self.hosteller_user,
            name="Hosteller One",
            registration_number="ANLHOST01",
            hostel=self.hostel,
            student_type=Student.HOSTELLER,
        )

        self.day_scholar_user = CustomUser.objects.create_user(
            email="dayscholar@example.com",
            password="pass12345",
            user_type="student",
        )
        self.day_scholar = Student.objects.create(
            user=self.day_scholar_user,
            name="Day Scholar One",
            registration_number="ANLDAY01",
            student_type=Student.DAY_SCHOLAR,
        )

    def _create_pass(self, user, resource, pass_date, current_step=1, valid=True, defaulter=False):
        now = timezone.now()
        user_pass = NightPass.objects.create(
            user=user,
            start_time=now.time(),
            end_time=now + timedelta(hours=3),
            campus_resource=resource,
        )
        NightPass.objects.filter(pass_id=user_pass.pass_id).update(
            date=pass_date,
            current_step=current_step,
            valid=valid,
            defaulter=defaulter,
        )
        user_pass.refresh_from_db()
        return user_pass

    def test_analytics_returns_200_for_admin(self):
        response = self.client.get(client_path("analytics"))
        self.assertEqual(response.status_code, 200)

    def test_analytics_kpis_match_expected_calculations(self):
        today = timezone.localdate()
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=4, valid=False)
        self._create_pass(self.day_scholar_user, self.day_scholar_resource, today, current_step=1, valid=True)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_passes"], 2)
        self.assertEqual(response.context["active_passes"], 1)
        self.assertEqual(response.context["completed_passes"], 1)
        self.assertEqual(response.context["defaulters"], 0)
        self.assertEqual(response.context["total_students"], Student.objects.count())

    def test_analytics_date_filter_scopes_passes(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(self.day_scholar_user, self.day_scholar_resource, yesterday, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.context["total_passes"], 1)
        self.assertEqual(response.context["from_date"], today.isoformat())
        self.assertEqual(response.context["to_date"], today.isoformat())

    def test_student_type_distribution_matches_population(self):
        response = self.client.get(client_path("analytics"))

        self.assertEqual(response.context["student_type_labels"], ["Hostellers", "Day Scholars"])
        hosteller_count = Student.objects.filter(student_type=Student.HOSTELLER).count()
        day_scholar_count = Student.objects.filter(student_type=Student.DAY_SCHOLAR).count()
        self.assertEqual(
            response.context["student_type_counts"],
            [hosteller_count, day_scholar_count],
        )

    def test_pass_type_distribution_matches_selected_range(self):
        today = timezone.localdate()
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(self.hosteller_user, self.outside_resource, today, current_step=1)
        self._create_pass(self.day_scholar_user, self.day_scholar_resource, today, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(
            response.context["pass_type_labels"],
            ["Hostel", "Outside Hostel", "Day Scholar"],
        )
        self.assertEqual(response.context["pass_type_counts"], [1, 1, 1])

    def test_pass_activity_outcome_breakdown_is_mutually_exclusive(self):
        today = timezone.localdate()
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=4, valid=False)
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=2, valid=True)
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1, valid=True)
        self._create_pass(self.day_scholar_user, self.day_scholar_resource, today, current_step=1, valid=True)
        self._create_pass(
            self.hosteller_user, self.hostel_resource, today, current_step=3, valid=True, defaulter=True
        )
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1, valid=False)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(
            response.context["outcome_labels"],
            [
                "Completed",
                "Currently in Library",
                "In Transit",
                "Waiting / Active Other",
                "Violation",
                "Expired / Closed Other",
            ],
        )
        completed, in_library, in_transit, waiting_other, violation, expired_closed = response.context[
            "outcome_counts"
        ]
        self.assertEqual(completed, 1)
        self.assertEqual(in_library, 1)
        self.assertEqual(in_transit, 1)
        self.assertEqual(violation, 1)
        self.assertEqual(expired_closed, 1)
        self.assertEqual(waiting_other, 1)
        self.assertEqual(sum(response.context["outcome_counts"]), 6)

    def test_null_defaulter_outcomes_use_state_buckets(self):
        today = timezone.localdate()
        cases = [
            (self.hosteller_user, self.hostel_resource, 4, False, "Completed"),
            (self.hosteller_user, self.hostel_resource, 2, True, "Currently in Library"),
            (self.hosteller_user, self.hostel_resource, 1, True, "In Transit"),
            (self.hosteller_user, self.hostel_resource, 3, True, "In Transit"),
            (self.hosteller_user, self.hostel_resource, 1, False, "Expired / Closed Other"),
            (self.day_scholar_user, self.day_scholar_resource, 1, True, "Waiting / Active Other"),
        ]
        for user, resource, step, valid, expected in cases:
            with self.subTest(step=step, valid=valid, pass_type=resource.default_pass_type):
                user_pass = self._create_pass(
                    user, resource, today, current_step=step, valid=valid, defaulter=None
                )
                self.assertIsNone(user_pass.defaulter)
                response = self.client.get(
                    client_path("analytics"),
                    {"from_date": today.isoformat(), "to_date": today.isoformat()},
                )
                outcomes = dict(zip(response.context["outcome_labels"], response.context["outcome_counts"]))
                self.assertEqual(outcomes[expected], 1)
                self.assertEqual(sum(outcomes.values()), 1)
                self.assertEqual(outcomes["Violation"], 0)
                if expected != "Waiting / Active Other":
                    self.assertEqual(outcomes["Waiting / Active Other"], 0)
                user_pass.delete()

    def test_analytics_with_no_passes_in_range_renders_without_error(self):
        future_date = timezone.localdate() + timedelta(days=365)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": future_date.isoformat(), "to_date": future_date.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_passes"], 0)
        self.assertEqual(sum(response.context["outcome_counts"]), 0)
        self.assertEqual(sum(response.context["pass_type_counts"]), 0)

    def test_hostel_chart_counts_hostel_and_outside_passes(self):
        today = timezone.localdate()
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(self.hosteller_user, self.outside_resource, today, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.context["hostel_labels"], [self.hostel.name])
        self.assertEqual(response.context["hostel_counts"], [2])

    def test_hostel_chart_excludes_day_scholar_passes(self):
        today = timezone.localdate()
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(self.day_scholar_user, self.day_scholar_resource, today, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.context["hostel_labels"], [self.hostel.name])
        self.assertEqual(response.context["hostel_counts"], [1])

    def test_hostel_chart_excludes_students_with_no_hostel(self):
        today = timezone.localdate()
        homeless_user = CustomUser.objects.create_user(
            email="homeless@example.com",
            password="pass12345",
            user_type="student",
        )
        Student.objects.create(
            user=homeless_user,
            name="Homeless Hosteller",
            registration_number="ANLNOHOSTEL01",
            hostel=None,
            student_type=Student.HOSTELLER,
        )
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(homeless_user, self.hostel_resource, today, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.context["hostel_labels"], [self.hostel.name])
        self.assertEqual(response.context["hostel_counts"], [1])

    def test_hostel_chart_respects_selected_date_range(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(self.hosteller_user, self.hostel_resource, yesterday, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.context["hostel_labels"], [self.hostel.name])
        self.assertEqual(response.context["hostel_counts"], [1])

    def test_hostel_chart_orders_by_count_descending(self):
        today = timezone.localdate()
        second_hostel = Hostel.objects.create(
            name="Second Analytics Hostel",
            contact_number="9999999997",
            email="second-analytics-hostel@example.com",
        )
        second_hosteller_user = CustomUser.objects.create_user(
            email="second-hosteller@example.com",
            password="pass12345",
            user_type="student",
        )
        Student.objects.create(
            user=second_hosteller_user,
            name="Second Hosteller",
            registration_number="ANLHOST02",
            hostel=second_hostel,
            student_type=Student.HOSTELLER,
        )

        # "Analytics Hostel" gets 2 passes, "Second Analytics Hostel" gets 1.
        self._create_pass(self.hosteller_user, self.hostel_resource, today, current_step=1)
        self._create_pass(self.hosteller_user, self.outside_resource, today, current_step=1)
        self._create_pass(second_hosteller_user, self.hostel_resource, today, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(
            response.context["hostel_labels"],
            [self.hostel.name, second_hostel.name],
        )
        self.assertEqual(response.context["hostel_counts"], [2, 1])

    def test_hostel_chart_excludes_day_scholar_pass_type_for_hosteller_student(self):
        today = timezone.localdate()
        # Inconsistent record: a HOSTELLER student (with a hostel assigned)
        # who nonetheless has a DAY_SCHOLAR-typed NightPass. The chart counts
        # by NightPass.pass_type, not just student_type, so this must be excluded.
        self._create_pass(self.hosteller_user, self.day_scholar_resource, today, current_step=1)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": today.isoformat(), "to_date": today.isoformat()},
        )

        self.assertEqual(response.context["hostel_labels"], [])
        self.assertEqual(response.context["hostel_counts"], [])

    def test_hostel_chart_empty_when_no_hosteller_passes_in_range(self):
        future_date = timezone.localdate() + timedelta(days=365)

        response = self.client.get(
            client_path("analytics"),
            {"from_date": future_date.isoformat(), "to_date": future_date.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["hostel_labels"], [])
        self.assertEqual(response.context["hostel_counts"], [])


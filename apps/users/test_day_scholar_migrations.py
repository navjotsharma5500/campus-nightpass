"""Verify additive migrations preserve historical student/user/pass identities."""
from datetime import time, timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class DayScholarMigrationTests(TransactionTestCase):
    def test_existing_rows_get_hosteller_defaults_without_history_changes(self):
        old_targets = [("users", "0026_violationauditlog"), ("nightpass", "0007_alter_hostel_name")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old_targets)
            old = executor.loader.project_state(old_targets).apps
            user = old.get_model("users", "CustomUser").objects.create(email="migration@example.com")
            student = old.get_model("users", "Student").objects.create(user_id=user.pk,
                registration_number="MIG001", name="Original", violation_flags=2, picture="https://example.com/p.jpg")
            resource = old.get_model("nightpass", "CampusResource").objects.create(name="Library",
                description="Original", max_capacity=10, start_time=time(0), end_time=time(23))
            old.get_model("users", "NightPass").objects.create(user_id=user.pk, campus_resource_id=resource.pk,
                pass_id="MIGPASS", start_time=time(20), end_time=timezone.now()+timedelta(hours=1), current_step=2)
            before_student = old.get_model("users", "Student").objects.values().get(pk=student.pk)
            before_pass = old.get_model("users", "NightPass").objects.values().get(pk="MIGPASS")
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            new = executor.loader.project_state(latest).apps
            after_student = new.get_model("users", "Student").objects.values().get(pk=student.pk)
            self.assertEqual(after_student.pop("student_type"), "HOSTELLER")
            self.assertEqual(after_student, before_student)
            self.assertEqual(new.get_model("nightpass", "CampusResource").objects.get(pk=resource.pk).audience_type, "HOSTELLER")
            self.assertEqual(new.get_model("nightpass", "CampusResource").objects.count(), 1)
            self.assertEqual(new.get_model("users", "NightPass").objects.values().get(pk="MIGPASS"), before_pass)
        finally:
            MigrationExecutor(connection).migrate(latest)

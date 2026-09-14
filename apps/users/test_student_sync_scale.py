"""Repeatable local workload; timings are reported, not asserted across hardware."""
from time import perf_counter

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.nightpass.models import Hostel
from apps.users.models import Student
from apps.users.services.student_sync import apply_sync, preview_sync


class StudentSyncScaleTests(TestCase):
    def test_15000_students(self):
        Hostel.objects.create(name="Scale", email="hostel@example.com", contact_number="123")
        rows = [{"email": f"scale{i}@example.com", "registration_number": f"S{i:05}",
                 "name": f"Student {i}", "hostel": "Scale", "room_number": str(i % 500),
                 "gender": "M", "contact_number": "1234567890", "parent_contact": "1234567890",
                 "year": "2", "picture": f"https://example.com/{i}.jpg"} for i in range(15000)]
        started = perf_counter()
        plan = preview_sync(list(rows[0]), rows, "full")
        self.assertFalse(plan.errors)
        self.assertEqual(Student.objects.count(), 0)
        print(f"\n15,000-row preview: {perf_counter() - started:.3f}s")
        workloads = [("full", rows),
                     ("full", [{**row, "name": "Updated " + row["name"], "gender": "F", "year": "3",
                                "room_number": "999", "contact_number": "9876543210", "parent_contact": "9876543210",
                                "picture": f'https://example.com/updated/{i}.jpg'} for i, row in enumerate(rows)]),
                     ("hostel", [{"email": row["email"], "hostel": "Scale", "room_number": "200"} for row in rows]),
                     ("picture", [{"email": row["email"], "picture": "https://example.com/new.jpg"} for row in rows])]
        for mode, batch in workloads:
            with CaptureQueriesContext(connection) as queries:
                counts, duration = apply_sync(list(batch[0]), batch, mode, actor="scale-test", filename="15000.csv")
            print(f"15,000-row {mode}: {duration:.3f}s, {len(queries)} queries")
            self.assertLess(len(queries), 1200)
            self.assertEqual(counts["valid_rows"], 15000)
        self.assertEqual(Student.objects.count(), 15000)

from django.test import SimpleTestCase

from .services.lifecycle import step_label


class LifecycleServiceTests(SimpleTestCase):
    def test_step_label_map(self):
        self.assertEqual(step_label(0), "Hostel Out")
        self.assertEqual(step_label(1), "Library In")
        self.assertEqual(step_label(2), "Library Out")
        self.assertEqual(step_label(3), "Hostel In")

    def test_unknown_step_label(self):
        self.assertEqual(step_label(99), "Valid Scan")

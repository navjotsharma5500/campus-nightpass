from django.core.management.base import BaseCommand

from ...services.deadline_evaluator import evaluate_active_pass_deadlines


class Command(BaseCommand):
    help = "Evaluate active passes for missed deadlines and expire defaulters."

    def handle(self, *args, **options):
        summary = evaluate_active_pass_deadlines()
        self.stdout.write(
            self.style.SUCCESS(
                "Deadline evaluation complete | "
                f"expired={summary['expired_passes']}, "
                f"missed_library_in={summary['missed_library_in']}, "
                f"missed_hostel_in={summary['missed_hostel_in']}"
            )
        )

from django.core.management.base import BaseCommand

from ...services.deadline_evaluator import evaluate_active_pass_deadlines


def check_defaulters():
    return evaluate_active_pass_deadlines()


class Command(BaseCommand):
    help = 'Check for defaulters using the unified deadline policy'

    def handle(self, *args, **options):
        summary = check_defaulters()
        self.stdout.write(
            self.style.SUCCESS(
                'Unified deadline evaluation complete | '
                f"expired={summary['expired_passes']}, "
                f"missed_library_in={summary['missed_library_in']}, "
                f"missed_library_out={summary['missed_library_out']}, "
                f"missed_hostel_in={summary['missed_hostel_in']}"
            )
        )

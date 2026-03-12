from django.db import transaction
from django.utils import timezone

from ..models import NightPass
from .pass_policy import apply_overdue_violation, expire_stale_passes, resolve_active_policy


@transaction.atomic
def evaluate_active_pass_deadlines(now=None):
    now = now or timezone.now()
    policy = resolve_active_policy(timezone.localdate(now))
    processed = {
        "expired_passes": 0,
        "missed_library_in": 0,
        "missed_library_out": 0,
        "missed_hostel_in": 0,
    }

    active_passes = NightPass.objects.select_related(
        "user__student",
        "user__student__hostel",
    ).filter(valid=True, date=timezone.localdate(now))

    for user_pass in active_passes:
        if apply_overdue_violation(user_pass, effective_now=now, policy=policy):
            if user_pass.current_step == 1:
                processed["missed_library_in"] += 1
            elif user_pass.current_step == 2:
                processed["missed_library_out"] += 1
            elif user_pass.current_step == 3:
                processed["missed_hostel_in"] += 1

    def expire_callback(user_pass, effective_now):
        if apply_overdue_violation(user_pass, effective_now=effective_now, policy=policy):
            if user_pass.current_step == 1:
                processed["missed_library_in"] += 1
            elif user_pass.current_step == 2:
                processed["missed_library_out"] += 1
            elif user_pass.current_step == 3:
                processed["missed_hostel_in"] += 1

    processed["expired_passes"] = expire_stale_passes(
        now=now,
        queryset=NightPass.objects.select_related("user__student", "user__student__hostel"),
        violation_callback=expire_callback,
    )

    return processed

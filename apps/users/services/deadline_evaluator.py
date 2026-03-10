from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from ...global_settings.models import Settings
from ..models import NightPass
from .violation_utils import append_violation


MISSED_LIBRARY_IN = "MISSED_LIBRARY_IN"
MISSED_LIBRARY_OUT = "MISSED_LIBRARY_OUT"
MISSED_HOSTEL_IN = "MISSED_HOSTEL_IN"
OUTSIDE_LIBRARY_IN_START = time(20, 0)
DEFAULT_LIBRARY_OUT_CUTOFF = time(23, 0)
DEFAULT_HOSTEL_OUT_LIBRARY_TIMER_MINUTES = 40


def _get_timers(student, policy):
    if policy and policy.enable_hostel_timers and student.hostel:
        return (
            int(student.hostel.frontend_checkin_timer or 0),
            int(policy.library_timer_for_hostel_out or DEFAULT_HOSTEL_OUT_LIBRARY_TIMER_MINUTES),
            int(student.hostel.backend_checkin_timer or 0),
        )
    if policy:
        return (
            int(policy.frontend_checkin_timer or 0),
            int(policy.library_timer_for_hostel_out or DEFAULT_HOSTEL_OUT_LIBRARY_TIMER_MINUTES),
            int(policy.backend_checkin_timer or 0),
        )
    return (0, DEFAULT_HOSTEL_OUT_LIBRARY_TIMER_MINUTES, 0)


def _outside_frontend_start(user_pass):
    base_dt = datetime.combine(user_pass.date, OUTSIDE_LIBRARY_IN_START)
    return timezone.make_aware(base_dt, timezone.get_current_timezone())


def _should_flag_missed_library_in(user_pass, now, frontend_timer_minutes, hostel_out_timer_minutes):
    if user_pass.pass_type == "OUTSIDE":
        start_at = _outside_frontend_start(user_pass)
        timer_minutes = hostel_out_timer_minutes
    else:
        start_at = user_pass.hostel_checkout_time
        timer_minutes = frontend_timer_minutes

    if not start_at:
        return False

    deadline = start_at + timedelta(minutes=timer_minutes)
    return now > deadline


def _should_flag_missed_hostel_in(user_pass, now, backend_timer_minutes):
    if not user_pass.library_out_time:
        return False
    deadline = user_pass.library_out_time + timedelta(minutes=backend_timer_minutes)
    return now > deadline


def _library_out_cutoff_time(policy):
    if policy and policy.library_out_cutoff_time:
        return policy.library_out_cutoff_time
    return DEFAULT_LIBRARY_OUT_CUTOFF


def _should_flag_missed_library_out(user_pass, now, cutoff_time):
    if user_pass.library_out_time:
        return False
    cutoff = timezone.make_aware(
        datetime.combine(user_pass.date, cutoff_time),
        timezone.get_current_timezone(),
    )
    return now > cutoff


@transaction.atomic
def evaluate_active_pass_deadlines(now=None):
    now = now or timezone.now()
    policy = Settings.objects.first()
    processed = {
        "expired_passes": 0,
        "missed_library_in": 0,
        "missed_library_out": 0,
        "missed_hostel_in": 0,
    }

    active_passes = NightPass.objects.select_related(
        "user__student",
        "user__student__hostel",
    ).filter(valid=True)
    library_out_cutoff_time = _library_out_cutoff_time(policy)

    for user_pass in active_passes:
        student = user_pass.user.student
        frontend_timer_minutes, hostel_out_timer_minutes, backend_timer_minutes = _get_timers(student, policy)
        was_defaulter = bool(user_pass.defaulter)

        reason_added = False

        if user_pass.current_step == 1 and _should_flag_missed_library_in(
            user_pass,
            now,
            frontend_timer_minutes,
            hostel_out_timer_minutes,
        ):
            reason_added = append_violation(
                user_pass,
                MISSED_LIBRARY_IN,
                "Required Library IN scan missed before deadline.",
                occurred_at=now,
            )
            if reason_added:
                processed["missed_library_in"] += 1

        elif user_pass.current_step == 2 and _should_flag_missed_library_out(user_pass, now, library_out_cutoff_time):
            reason_added = append_violation(
                user_pass,
                MISSED_LIBRARY_OUT,
                "Required Library OUT scan missed before configured cutoff time.",
                occurred_at=now,
            )
            if reason_added:
                processed["missed_library_out"] += 1

        elif user_pass.current_step == 3 and _should_flag_missed_hostel_in(user_pass, now, backend_timer_minutes):
            reason_added = append_violation(
                user_pass,
                MISSED_HOSTEL_IN,
                "Required Hostel IN scan missed before deadline.",
                occurred_at=now,
            )
            if reason_added:
                processed["missed_hostel_in"] += 1

        if reason_added:
            user_pass.save(
                update_fields=[
                    "defaulter",
                    "defaulter_remarks",
                    "violation_code",
                    "violation_time",
                ]
            )

            if not was_defaulter:
                student.violation_flags += 1
                student.save(update_fields=["violation_flags"])

    return processed

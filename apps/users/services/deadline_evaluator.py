from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from ...global_settings.models import Settings
from ..models import NightPass


MISSED_LIBRARY_IN = "MISSED_LIBRARY_IN"
MISSED_HOSTEL_IN = "MISSED_HOSTEL_IN"


def _get_timers(student, policy):
    if policy and policy.enable_hostel_timers and student.hostel:
        return (
            int(student.hostel.frontend_checkin_timer or 0),
            int(student.hostel.backend_checkin_timer or 0),
        )
    if policy:
        return (
            int(policy.frontend_checkin_timer or 0),
            int(policy.backend_checkin_timer or 0),
        )
    return (0, 0)


def _append_defaulter_reason(user_pass, reason_code, human_message):
    token = f"[{reason_code}]"
    existing = user_pass.defaulter_remarks or ""
    if token in existing:
        return False

    combined = f"{token} {human_message}".strip()
    user_pass.defaulter_remarks = f"{existing} | {combined}" if existing else combined
    return True


def _outside_frontend_start(user_pass):
    base_dt = datetime.combine(user_pass.date, user_pass.start_time)
    return timezone.make_aware(base_dt, timezone.get_current_timezone())


def _should_flag_missed_library_in(user_pass, now, frontend_timer_minutes):
    if user_pass.pass_type == "OUTSIDE":
        start_at = _outside_frontend_start(user_pass)
    else:
        start_at = user_pass.hostel_checkout_time

    if not start_at:
        return False

    deadline = start_at + timedelta(minutes=frontend_timer_minutes)
    return now > deadline


def _should_flag_missed_hostel_in(user_pass, now, backend_timer_minutes):
    if not user_pass.library_out_time:
        return False
    deadline = user_pass.library_out_time + timedelta(minutes=backend_timer_minutes)
    return now > deadline


@transaction.atomic
def evaluate_active_pass_deadlines(now=None):
    now = now or timezone.now()
    policy = Settings.objects.first()
    processed = {
        "expired_passes": 0,
        "missed_library_in": 0,
        "missed_hostel_in": 0,
    }

    active_passes = NightPass.objects.select_related(
        "user__student",
        "user__student__hostel",
    ).filter(valid=True)

    for user_pass in active_passes:
        student = user_pass.user.student
        frontend_timer_minutes, backend_timer_minutes = _get_timers(student, policy)

        reason_added = False

        if user_pass.current_step == 1 and _should_flag_missed_library_in(user_pass, now, frontend_timer_minutes):
            reason_added = _append_defaulter_reason(
                user_pass,
                MISSED_LIBRARY_IN,
                "Required Library IN scan missed before deadline.",
            )
            if reason_added:
                processed["missed_library_in"] += 1

        elif user_pass.current_step == 3 and _should_flag_missed_hostel_in(user_pass, now, backend_timer_minutes):
            reason_added = _append_defaulter_reason(
                user_pass,
                MISSED_HOSTEL_IN,
                "Required Hostel IN scan missed before deadline.",
            )
            if reason_added:
                processed["missed_hostel_in"] += 1

        if reason_added:
            user_pass.defaulter = True
            user_pass.valid = False
            user_pass.save(update_fields=["defaulter", "defaulter_remarks", "valid"])

            student.violation_flags += 1
            student.has_booked = False
            student.save(update_fields=["violation_flags", "has_booked"])

            processed["expired_passes"] += 1

    return processed

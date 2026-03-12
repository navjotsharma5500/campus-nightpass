from datetime import timedelta

from django.utils import timezone

from ...users.services.pass_policy import (
    STEP_COMPLETED,
    STEP_HOSTEL_IN,
    STEP_HOSTEL_OUT,
    STEP_LIBRARY_IN,
    STEP_LIBRARY_OUT,
    get_library_out_cutoff_time,
    library_out_cutoff_datetime,
    outside_library_in_start,
    required_location,
    resolve_active_policy,
    resolve_transit_timers,
    step_label,
    violation_code_for_step,
)
from ...users.services.violation_utils import append_violation


def _mark_violation(user_pass, student, step, remark, occurred_at=None):
    code = violation_code_for_step(step)
    if not code:
        return False

    added = append_violation(user_pass, code, remark, occurred_at=occurred_at)
    if added:
        student.violation_flags += 1
    return added


def transition_checkout_from_hostel(user_pass):
    if user_pass.current_step != STEP_HOSTEL_OUT:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Invalid step for Hostel Exit."}

    now = timezone.now()
    student = user_pass.user.student

    student.is_checked_in = False
    user_pass.hostel_checkout_time = now
    user_pass.current_step = STEP_LIBRARY_IN

    student.save(update_fields=["is_checked_in"])
    user_pass.save(update_fields=["hostel_checkout_time", "current_step"])

    return {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Hostel Exit Authorized."}


def transition_checkin_to_library(user_pass):
    if user_pass.current_step != STEP_LIBRARY_IN:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Exit hostel first."}

    now = timezone.now()
    student = user_pass.user.student
    frontend_timer, hostel_out_library_timer, _ = resolve_transit_timers(student, now=now)

    transit_start = user_pass.hostel_checkout_time
    allowed_minutes = frontend_timer
    if user_pass.pass_type == "OUTSIDE":
        transit_start = outside_library_in_start(user_pass)
        allowed_minutes = hostel_out_library_timer

    violation_occurred = False
    if transit_start:
        transit = now - transit_start
        if transit > timedelta(minutes=allowed_minutes):
            violation_occurred = _mark_violation(
                user_pass,
                student,
                STEP_LIBRARY_IN,
                f"Violation: Library IN Late ({int(transit.total_seconds() // 60)} mins)",
                occurred_at=now,
            )
            if violation_occurred:
                student.save(update_fields=["violation_flags"])

    user_pass.library_in_time = now
    user_pass.current_step = STEP_LIBRARY_OUT
    user_pass.save(
        update_fields=[
            "library_in_time",
            "current_step",
            "defaulter",
            "defaulter_remarks",
            "violation_code",
            "violation_time",
        ]
    )

    payload = {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Checked into Library."}
    if violation_occurred:
        payload["violation_occurred"] = True
        payload["violation_message"] = "Violation occurred for late Library IN scan."
    return payload


def transition_checkout_from_library(user_pass):
    if user_pass.current_step != STEP_LIBRARY_OUT:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Student not inside resource."}

    now = timezone.now()
    student = user_pass.user.student
    policy = resolve_active_policy(timezone.localdate(now))
    cutoff_at = library_out_cutoff_datetime(user_pass, policy=policy)

    violation_occurred = False
    if now > cutoff_at:
        violation_occurred = _mark_violation(
            user_pass,
            student,
            STEP_LIBRARY_OUT,
            f"Violation: Library OUT Late (cutoff {get_library_out_cutoff_time(policy).strftime('%H:%M')})",
            occurred_at=now,
        )
        if violation_occurred:
            student.save(update_fields=["violation_flags"])

    user_pass.library_out_time = now
    user_pass.current_step = STEP_HOSTEL_IN
    user_pass.save(
        update_fields=[
            "library_out_time",
            "current_step",
            "defaulter",
            "defaulter_remarks",
            "violation_code",
            "violation_time",
        ]
    )

    payload = {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Library Exit recorded."}
    if violation_occurred:
        payload["violation_occurred"] = True
        payload["violation_message"] = "Violation occurred for late Library OUT scan."
    return payload


def transition_checkin_to_hostel(student):
    user_pass = student.user.nightpass_set.filter(valid=True).order_by("-date", "-start_time").first()
    if not user_pass or user_pass.current_step != STEP_HOSTEL_IN:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Must exit library first."}

    now = timezone.now()
    _, _, backend_timer = resolve_transit_timers(student, now=now)

    violation_occurred = False
    if user_pass.library_out_time:
        transit = now - user_pass.library_out_time
        if transit > timedelta(minutes=backend_timer):
            violation_occurred = _mark_violation(
                user_pass,
                student,
                STEP_HOSTEL_IN,
                f"Late return ({int(transit.total_seconds() // 60)} mins)",
                occurred_at=now,
            )

    student.is_checked_in = True
    student.hostel_checkin_time = now
    student.has_booked = False
    student.save(update_fields=["is_checked_in", "hostel_checkin_time", "has_booked", "violation_flags"])

    user_pass.hostel_checkin_time = now
    user_pass.current_step = STEP_COMPLETED
    user_pass.valid = False
    user_pass.save(
        update_fields=[
            "hostel_checkin_time",
            "current_step",
            "valid",
            "defaulter",
            "defaulter_remarks",
            "violation_code",
            "violation_time",
        ]
    )

    payload = {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Hostel Entry Success. Pass Closed."}
    if violation_occurred:
        payload["violation_occurred"] = True
        payload["violation_message"] = "Violation occurred for late Hostel IN scan."
    return payload

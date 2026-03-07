from datetime import timedelta
from django.utils import timezone

from ...global_settings.models import Settings


STEP_LABELS = {
    0: "Hostel Out",
    1: "Library In",
    2: "Library Out",
    3: "Hostel In",
}

DEFAULT_TRANSIT_LIMIT_MINUTES = 40


def required_location(user_pass):
    if user_pass.pass_type == "OUTSIDE":
        mapping = {
            1: "LIBRARY",
            2: "LIBRARY",
            3: "HOSTEL",
        }
    else:
        mapping = {
            0: "HOSTEL",
            1: "LIBRARY",
            2: "LIBRARY",
            3: "HOSTEL",
        }
    return mapping.get(user_pass.current_step)


def step_label(step):
    return STEP_LABELS.get(step, "Valid Scan")


def _resolve_active_policy(current_date=None):
    """
    Resolve the active policy for a given date.
    If date-range fields are present, use them.
    Otherwise, fallback to latest Settings row.
    """
    current_date = current_date or timezone.localdate()
    queryset = Settings.objects.all().order_by("-pk")

    model_fields = {field.name for field in Settings._meta.get_fields()}
    if {"start_date", "end_date"}.issubset(model_fields):
        dated = queryset.filter(start_date__lte=current_date, end_date__gte=current_date).first()
        if dated:
            return dated

    return queryset.first()


def _resolve_transit_timers(student, now=None):
    now = now or timezone.now()
    policy = _resolve_active_policy(current_date=timezone.localdate(now))

    frontend_timer = None
    backend_timer = None

    if policy:
        frontend_timer = policy.frontend_checkin_timer
        backend_timer = policy.backend_checkin_timer

        if policy.enable_hostel_timers and student.hostel:
            hostel_frontend = student.hostel.frontend_checkin_timer
            hostel_backend = student.hostel.backend_checkin_timer
            if hostel_frontend is not None:
                frontend_timer = hostel_frontend
            if hostel_backend is not None:
                backend_timer = hostel_backend

    frontend_timer = int(frontend_timer) if frontend_timer is not None else DEFAULT_TRANSIT_LIMIT_MINUTES
    backend_timer = int(backend_timer) if backend_timer is not None else DEFAULT_TRANSIT_LIMIT_MINUTES
    return frontend_timer, backend_timer


def _mark_violation(user_pass, student, remark):
    user_pass.defaulter = True
    user_pass.defaulter_remarks = (
        f"{user_pass.defaulter_remarks} | {remark}"
        if user_pass.defaulter_remarks else remark
    )
    student.violation_flags += 1


def transition_checkout_from_hostel(user_pass):
    if user_pass.current_step != 0:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Invalid step for Hostel Exit."}

    now = timezone.now()
    student = user_pass.user.student

    student.is_checked_in = False
    user_pass.hostel_checkout_time = now
    user_pass.current_step = 1

    student.save(update_fields=["is_checked_in"])
    user_pass.save(update_fields=["hostel_checkout_time", "current_step"])

    return {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Hostel Exit Authorized."}


def transition_checkin_to_library(user_pass):
    if user_pass.current_step != 1:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Exit hostel first."}

    now = timezone.now()
    student = user_pass.user.student
    frontend_timer, _ = _resolve_transit_timers(student, now=now)

    if user_pass.hostel_checkout_time:
        transit = now - user_pass.hostel_checkout_time
        if transit > timedelta(minutes=frontend_timer):
            _mark_violation(user_pass, student, f"Late arrival ({transit.seconds // 60} mins)")
            student.save(update_fields=["violation_flags"])

    user_pass.library_in_time = now
    user_pass.current_step = 2
    user_pass.save(update_fields=["library_in_time", "current_step", "defaulter", "defaulter_remarks"])

    return {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Checked into Library."}


def transition_checkout_from_library(user_pass):
    if user_pass.current_step != 2:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Student not inside resource."}

    now = timezone.now()

    user_pass.library_out_time = now
    user_pass.current_step = 3
    user_pass.save(update_fields=["library_out_time", "current_step"])

    return {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Library Exit recorded."}


def transition_checkin_to_hostel(student):
    user_pass = student.user.nightpass_set.filter(valid=True).first()
    if not user_pass or user_pass.current_step != 3:
        return {"status": False, "reason_code": "INVALID_TRANSITION", "message": "Must exit library first."}

    now = timezone.now()
    _, backend_timer = _resolve_transit_timers(student, now=now)

    if user_pass.library_out_time:
        transit = now - user_pass.library_out_time
        if transit > timedelta(minutes=backend_timer):
            _mark_violation(user_pass, student, f"Late return ({transit.seconds // 60} mins)")

    student.is_checked_in = True
    student.hostel_checkin_time = now
    student.has_booked = False
    student.save(update_fields=["is_checked_in", "hostel_checkin_time", "has_booked", "violation_flags"])

    user_pass.hostel_checkin_time = now
    user_pass.current_step = 4
    user_pass.valid = False
    user_pass.save(
        update_fields=[
            "hostel_checkin_time",
            "current_step",
            "valid",
            "defaulter",
            "defaulter_remarks",
        ]
    )

    return {"status": True, "reason_code": "TRANSITION_APPLIED", "message": "Hostel Entry Success. Pass Closed."}

from datetime import datetime, time, timedelta

from django.utils import timezone

from ...global_settings.models import Settings
from ..models import NightPass
from .violation_utils import append_violation


STEP_HOSTEL_OUT = 0
STEP_LIBRARY_IN = 1
STEP_LIBRARY_OUT = 2
STEP_HOSTEL_IN = 3
STEP_COMPLETED = 4

DEFAULT_TRANSIT_LIMIT_MINUTES = 30
DEFAULT_SCAN_START_TIME = time(20, 0)
DEFAULT_SCAN_END_TIME = time(22, 30)
DEFAULT_LIBRARY_OUT_CUTOFF = time(23, 0)
DEFAULT_SLOT_CANCEL_TIME = time(20, 0)
OUTSIDE_LIBRARY_IN_START = time(20, 0)

LATE_LIBRARY_IN = "LATE_LIBRARY_IN"
LATE_LIBRARY_OUT = "LATE_LIBRARY_OUT"
LATE_HOSTEL_IN = "LATE_HOSTEL_IN"

STEP_LABELS = {
    STEP_HOSTEL_OUT: "Hostel Out",
    STEP_LIBRARY_IN: "Library In",
    STEP_LIBRARY_OUT: "Library Out",
    STEP_HOSTEL_IN: "Hostel In",
}


def resolve_active_policy(current_date=None):
    current_date = current_date or timezone.localdate()
    queryset = Settings.objects.all().order_by("-pk")

    model_fields = {field.name for field in Settings._meta.get_fields()}
    if {"start_date", "end_date"}.issubset(model_fields):
        dated = queryset.filter(start_date__lte=current_date, end_date__gte=current_date).first()
        if dated:
            return dated

    return queryset.first()


def get_scan_window(now=None):
    now = now or timezone.now()
    policy = resolve_active_policy(timezone.localdate(now))
    start = policy.scan_start_time if policy and policy.scan_start_time else DEFAULT_SCAN_START_TIME
    end = policy.scan_end_time if policy and policy.scan_end_time else DEFAULT_SCAN_END_TIME
    return start, end


def get_slot_cancel_time(policy=None):
    policy = policy or resolve_active_policy()
    if policy and policy.slot_cancel_timer:
        return policy.slot_cancel_timer
    return DEFAULT_SLOT_CANCEL_TIME


def get_library_out_cutoff_time(policy=None):
    policy = policy or resolve_active_policy()
    if policy and policy.library_out_cutoff_time:
        return policy.library_out_cutoff_time
    return DEFAULT_LIBRARY_OUT_CUTOFF


def resolve_transit_timers(student, now=None):
    now = now or timezone.now()
    policy = resolve_active_policy(timezone.localdate(now))

    frontend_timer = None
    hostel_out_library_timer = None
    backend_timer = None

    if policy:
        frontend_timer = policy.frontend_checkin_timer
        hostel_out_library_timer = policy.library_timer_for_hostel_out
        backend_timer = policy.backend_checkin_timer

        if policy.enable_hostel_timers and student.hostel:
            if student.hostel.frontend_checkin_timer is not None:
                frontend_timer = student.hostel.frontend_checkin_timer
            if student.hostel.backend_checkin_timer is not None:
                backend_timer = student.hostel.backend_checkin_timer

    frontend_timer = int(frontend_timer) if frontend_timer not in (None, 0) else DEFAULT_TRANSIT_LIMIT_MINUTES
    hostel_out_library_timer = int(hostel_out_library_timer) if hostel_out_library_timer not in (None, 0) else DEFAULT_TRANSIT_LIMIT_MINUTES
    backend_timer = int(backend_timer) if backend_timer not in (None, 0) else DEFAULT_TRANSIT_LIMIT_MINUTES
    return frontend_timer, hostel_out_library_timer, backend_timer


def required_location(user_pass):
    if user_pass.pass_type == "OUTSIDE":
        mapping = {
            STEP_LIBRARY_IN: "LIBRARY",
            STEP_LIBRARY_OUT: "LIBRARY",
            STEP_HOSTEL_IN: "HOSTEL",
        }
    else:
        mapping = {
            STEP_HOSTEL_OUT: "HOSTEL",
            STEP_LIBRARY_IN: "LIBRARY",
            STEP_LIBRARY_OUT: "LIBRARY",
            STEP_HOSTEL_IN: "HOSTEL",
        }
    return mapping.get(user_pass.current_step)


def step_label(step):
    return STEP_LABELS.get(step, "Valid Scan")


def has_any_scan_activity(user_pass):
    return any([
        user_pass.hostel_checkout_time,
        user_pass.library_in_time,
        user_pass.library_out_time,
        user_pass.hostel_checkin_time,
    ])


def outside_library_in_start(user_pass):
    return timezone.make_aware(
        datetime.combine(user_pass.date, OUTSIDE_LIBRARY_IN_START),
        timezone.get_current_timezone(),
    )


def library_out_cutoff_datetime(user_pass, policy=None):
    cutoff_time = get_library_out_cutoff_time(policy=policy)
    return timezone.make_aware(
        datetime.combine(user_pass.date, cutoff_time),
        timezone.get_current_timezone(),
    )


def violation_code_for_step(step):
    mapping = {
        STEP_LIBRARY_IN: LATE_LIBRARY_IN,
        STEP_LIBRARY_OUT: LATE_LIBRARY_OUT,
        STEP_HOSTEL_IN: LATE_HOSTEL_IN,
    }
    return mapping.get(step)


def apply_overdue_violation(user_pass, effective_now=None, policy=None):
    effective_now = effective_now or timezone.now()
    policy = policy or resolve_active_policy(timezone.localdate(effective_now))
    student = user_pass.user.student
    frontend_timer, hostel_out_library_timer, backend_timer = resolve_transit_timers(student, now=effective_now)
    added = False

    if user_pass.current_step == STEP_LIBRARY_IN:
        start_at = user_pass.hostel_checkout_time
        allowed_minutes = frontend_timer
        if user_pass.pass_type == "OUTSIDE":
            start_at = outside_library_in_start(user_pass)
            allowed_minutes = hostel_out_library_timer
        if start_at and effective_now > start_at + timedelta(minutes=allowed_minutes):
            added = append_violation(
                user_pass,
                violation_code_for_step(STEP_LIBRARY_IN),
                "Required Library IN scan missed before deadline.",
                occurred_at=effective_now,
            )

    elif user_pass.current_step == STEP_LIBRARY_OUT:
        cutoff_at = library_out_cutoff_datetime(user_pass, policy=policy)
        if effective_now > cutoff_at:
            added = append_violation(
                user_pass,
                violation_code_for_step(STEP_LIBRARY_OUT),
                f"Required Library OUT scan missed before cutoff {get_library_out_cutoff_time(policy).strftime('%H:%M')}.",
                occurred_at=effective_now,
            )

    elif user_pass.current_step == STEP_HOSTEL_IN and user_pass.library_out_time:
        deadline = user_pass.library_out_time + timedelta(minutes=backend_timer)
        if effective_now > deadline:
            added = append_violation(
                user_pass,
                violation_code_for_step(STEP_HOSTEL_IN),
                "Required Hostel IN scan missed before deadline.",
                occurred_at=effective_now,
            )

    if added:
        student.violation_flags += 1
        student.save(update_fields=["violation_flags"])
        user_pass.save(update_fields=["defaulter", "defaulter_remarks", "violation_code", "violation_time"])
    return added


def _close_pass(user_pass, now=None):
    now = now or timezone.now()
    student = user_pass.user.student

    student.has_booked = False
    student.is_checked_in = True
    student.hostel_checkin_time = student.hostel_checkin_time or now
    student.save(update_fields=["has_booked", "is_checked_in", "hostel_checkin_time"])

    user_pass.valid = False
    user_pass.save(update_fields=["valid"])


def expire_stale_passes(now=None, queryset=None, violation_callback=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    queryset = queryset or NightPass.objects.select_related("user__student", "user__student__hostel")
    stale_passes = queryset.filter(valid=True, date__lt=today).order_by("date", "start_time")

    expired = 0
    callback = violation_callback or apply_overdue_violation
    for user_pass in stale_passes:
        callback(user_pass, now)
        _close_pass(user_pass, now=now)
        expired += 1
    return expired


def get_active_pass_for_user(user, now=None):
    now = now or timezone.now()
    expire_stale_passes(now=now, queryset=NightPass.objects.filter(user=user))
    return (
        NightPass.objects.filter(user=user, valid=True)
        .select_related("campus_resource", "user__student", "user__student__hostel")
        .order_by("-date", "-start_time")
        .first()
    )


def get_dashboard_status(user_pass, max_violations=None, now=None):
    now = now or timezone.now()
    student = user_pass.user.student
    if max_violations is not None and student.violation_flags >= max_violations:
        return "Block"
    if user_pass.defaulter:
        return "Violation"
    if user_pass.valid and user_pass.date < timezone.localdate(now):
        return "Expired"
    if not user_pass.valid and user_pass.current_step != STEP_COMPLETED:
        return "Expired"

    mapping = {
        STEP_HOSTEL_OUT: "Booked",
        STEP_LIBRARY_IN: "Hostel Out" if user_pass.pass_type != "OUTSIDE" else "Booked",
        STEP_LIBRARY_OUT: "Library IN",
        STEP_HOSTEL_IN: "Library Out",
        STEP_COMPLETED: "Hostel IN",
    }
    return mapping.get(user_pass.current_step, "Booked")


def get_scanner_status(user_pass, now=None):
    now = now or timezone.now()
    if user_pass.defaulter:
        return "Violation"
    if user_pass.valid and user_pass.date < timezone.localdate(now):
        return "Expired"
    if not user_pass.valid and user_pass.current_step != STEP_COMPLETED:
        return "Expired"

    mapping = {
        STEP_HOSTEL_OUT: "Night Pass Approved",
        STEP_LIBRARY_IN: "Hostel OUT" if user_pass.pass_type != "OUTSIDE" else "Night Pass Approved",
        STEP_LIBRARY_OUT: "Library IN",
        STEP_HOSTEL_IN: "Library OUT",
        STEP_COMPLETED: "Returned to Hostel",
    }
    return mapping.get(user_pass.current_step, "Night Pass Approved")



from datetime import time
from django.utils import timezone

from ...users.models import NightPass, Student
from ...global_settings.models import Settings
from .lifecycle import (
    required_location,
    step_label,
    transition_checkout_from_hostel,
    transition_checkin_to_library,
    transition_checkout_from_library,
    transition_checkin_to_hostel,
)


DEFAULT_SCAN_START_TIME = time(20, 0)
DEFAULT_SCAN_END_TIME = time(22, 30)


def _error(reason_code, message):
    return {"status": False, "reason_code": reason_code, "message": message}


def _format_time(value):
    return value.strftime("%I:%M %p").lstrip("0")


def _is_within_window(current_time, start_time, end_time):
    if start_time <= end_time:
        return start_time <= current_time <= end_time
    return current_time >= start_time or current_time <= end_time


def _resolve_active_policy(current_date=None):
    current_date = current_date or timezone.localdate()
    queryset = Settings.objects.all().order_by("-pk")

    model_fields = {field.name for field in Settings._meta.get_fields()}
    if {"start_date", "end_date"}.issubset(model_fields):
        active = queryset.filter(start_date__lte=current_date, end_date__gte=current_date).first()
        if active:
            return active
    return queryset.first()


def get_scan_window(now=None):
    now = now or timezone.now()
    policy = _resolve_active_policy(timezone.localdate(now))

    start = policy.scan_start_time if policy and policy.scan_start_time else DEFAULT_SCAN_START_TIME
    end = policy.scan_end_time if policy and policy.scan_end_time else DEFAULT_SCAN_END_TIME
    return start, end


def _success_payload(student, user_pass, result):
    pic_url = str(student.picture) if student.picture else "https://static.vecteezy.com/system/resources/previews/005/129/844/non_2x/profile-user-icon-isolated-on-white-background-eps10-free-vector.jpg"
    result.update({
        "user": {
            "name": student.name,
            "registration_number": student.registration_number,
            "hostel": student.hostel.name if student.hostel else "N/A",
            "picture": pic_url,
        },
        "task": {"check_in": False, "check_out": False},
        "user_pass": {"pass_id": user_pass.pk},
    })
    return result


def is_scan_window_open(now=None):
    now = now or timezone.localtime(timezone.now())
    start, end = get_scan_window(now)
    return _is_within_window(now.time(), start, end)


def resolve_scanner_context(user):
    security_profile = getattr(user, "security", None)
    if not security_profile:
        return None

    if security_profile.scanner_type == "HOSTEL":
        return {"location": "HOSTEL", "hostel_id": security_profile.hostel_id}
    return {"location": "LIBRARY", "hostel_id": None}


def scanner_location_label(user):
    context = resolve_scanner_context(user)
    if not context:
        return "Unknown"
    return "Library" if context["location"] == "LIBRARY" else "Hostel"


def process_scan(registration_number, user, now=None):
    now = now or timezone.localtime(timezone.now())
    scanner_context = resolve_scanner_context(user)
    if not scanner_context:
        return _error("SECURITY_PROFILE_MISSING", "Security profile missing.")

    if not registration_number:
        return _error("REGISTRATION_NUMBER_MISSING", "Registration number missing.")

    if not is_scan_window_open(now):
        start, end = get_scan_window(now)
        return _error(
            "SCAN_WINDOW_CLOSED",
            f"Scanning is allowed only between {_format_time(start)} and {_format_time(end)}.",
        )

    try:
        student = Student.objects.select_related("user", "hostel").get(registration_number=registration_number)
    except Student.DoesNotExist:
        return _error("STUDENT_NOT_FOUND", "Student not found.")

    user_pass = NightPass.objects.filter(user=student.user, valid=True).first()
    if not user_pass:
        return _error("NO_ACTIVE_PASS", "No active pass found for this student.")

    expected_location = required_location(user_pass)
    if expected_location is None:
        return _error("INVALID_PASS_STATE", "Invalid pass state for scanning.")

    if scanner_context["location"] != expected_location:
        return _error(
            "WRONG_SCANNER_LOCATION",
            f"This scan must happen at {expected_location.title()} ({step_label(user_pass.current_step)}).",
        )

    if scanner_context["location"] == "HOSTEL" and scanner_context["hostel_id"] and student.hostel_id and scanner_context["hostel_id"] != student.hostel_id:
        return _error("WRONG_HOSTEL_SCANNER", "This student belongs to a different hostel scanner.")

    if user_pass.current_step == 0:
        result = transition_checkout_from_hostel(user_pass)
    elif user_pass.current_step == 1:
        result = transition_checkin_to_library(user_pass)
    elif user_pass.current_step == 2:
        result = transition_checkout_from_library(user_pass)
    elif user_pass.current_step == 3:
        result = transition_checkin_to_hostel(student)
        user_pass.refresh_from_db()
    else:
        return _error("INVALID_PASS_STATE", "Invalid pass state for scanning.")

    if result.get("status"):
        return _success_payload(student, user_pass, result)
    return result

from datetime import date
from django.utils import timezone

from ...global_settings.models import Settings
from ...users.models import NightPass


def _response(reason_code, message):
    return {"status": False, "reason_code": reason_code, "message": message}


def _is_within_booking_window(current_time, start_time, end_time):
    if start_time <= end_time:
        return start_time <= current_time <= end_time
    return current_time >= start_time or current_time <= end_time


def _format_booking_time(value):
    return value.strftime("%I:%M %p").lstrip("0")


def validate_booking_policy(student, campus_resource):
    policy = Settings.objects.first()
    if not policy:
        return _response("NO_ACTIVE_POLICY", "No active booking policy found.")

    if (campus_resource.is_display is False) or not campus_resource.is_booking:
        return _response("BOOKING_NOT_AVAILABLE", "Booking is currently not available for this resource.")

    if campus_resource.booking_complete:
        return _response("CAPACITY_FULL", "All slots are booked for today!")

    now = timezone.localtime(timezone.now())

    weekday_flags = {
        0: policy.allow_monday,
        1: policy.allow_tuesday,
        2: policy.allow_wednesday,
        3: policy.allow_thursday,
        4: policy.allow_friday,
        5: policy.allow_saturday,
        6: policy.allow_sunday,
    }
    if not weekday_flags.get(now.weekday(), True):
        return _response("LIBRARY_CLOSED_TODAY", "Library is closed today")

    if not _is_within_booking_window(now.time(), campus_resource.start_time, campus_resource.end_time):
        return _response(
            "OUTSIDE_BOOKING_WINDOW",
            f"Please book between {_format_booking_time(campus_resource.start_time)} and {_format_booking_time(campus_resource.end_time)}.",
        )

    if policy.last_out_from_hostel and now.time() > policy.last_out_from_hostel:
        return _response("TIME_OVER_LAST_OUT_FROM_HOSTEL", "Time is over.")

    if int(student.violation_flags) >= int(policy.max_violation_count):
        return _response(
            "BLOCKED_MAX_VIOLATIONS",
            "Nightpass facility has been temporarily suspended! Contact DOSA office for further details.",
        )

    if policy.enable_gender_ratio:
        if student.gender == "male":
            male_count = NightPass.objects.filter(
                valid=True,
                campus_resource=campus_resource,
                user__student__gender="male",
                date=date.today(),
            ).count()
            if (male_count >= policy.male_ratio * campus_resource.max_capacity) or policy.male_ratio == 0:
                return _response("GENDER_QUOTA_FULL", "All slots are booked for today!")
        elif student.gender == "female":
            female_count = NightPass.objects.filter(
                valid=True,
                campus_resource=campus_resource,
                user__student__gender="female",
                date=date.today(),
            ).count()
            if (female_count >= policy.female_ratio * campus_resource.max_capacity) or policy.female_ratio == 0:
                return _response("GENDER_QUOTA_FULL", "All slots are booked for today!")

    if policy.enable_yearwise_limits:
        year_limits = {
            "1": policy.first_year,
            "2": policy.second_year,
            "3": policy.third_year,
            "4": policy.fourth_year,
        }
        student_year = str(student.year) if student.year is not None else ""
        configured_limit = year_limits.get(student_year)
        if configured_limit is not None:
            year_count = NightPass.objects.filter(
                valid=True,
                campus_resource=campus_resource,
                user__student__year=student_year,
                date=date.today(),
            ).count()
            if year_count >= configured_limit:
                return _response("YEAR_QUOTA_FULL", "All slots are booked for today!")

    if policy.enable_hostel_limits and student.hostel:
        hostel_count = NightPass.objects.filter(
            valid=True,
            campus_resource=campus_resource,
            user__student__hostel=student.hostel,
            date=date.today(),
        ).count()
        if hostel_count >= student.hostel.max_students_allowed:
            return _response("HOSTEL_QUOTA_FULL", "All slots are booked for today!")

    campus_resource.refresh_from_db(fields=["slots_booked", "max_capacity"])
    if campus_resource.slots_booked >= campus_resource.max_capacity:
        return _response("CAPACITY_FULL", f"No more slots available for {campus_resource.name}!")

    return None

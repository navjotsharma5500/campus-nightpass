from datetime import datetime, date
import random
import string

from django.db import transaction
from django.utils import timezone

from ...users.models import NightPass
from ...users.services.pass_policy import get_active_pass_for_user
from .booking_policy import validate_booking_policy


def _response(reason_code, message):
    return {"status": False, "reason_code": reason_code, "message": message}


def _existing_pass_blocker(user, now=None):
    active_pass = get_active_pass_for_user(user, now=now)
    if not active_pass:
        return None

    if active_pass.hostel_checkout_time or active_pass.library_in_time or active_pass.library_out_time:
        return _response(
            "ACTIVE_PASS_IN_USE",
            f"New slot can be booked only after the current pass for {active_pass.campus_resource} is closed.",
        )

    return _response(
        "ACTIVE_PASS_EXISTS",
        f"Cancel the booking for {active_pass.campus_resource} to book a new slot!",
    )


def _generate_pass_id():
    while True:
        pass_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        if not NightPass.objects.filter(pass_id=pass_id).exists():
            return pass_id


@transaction.atomic
def create_pass_for_student(user, campus_resource):
    now = timezone.localtime(timezone.now())

    blocker = _existing_pass_blocker(user, now=now)
    if blocker:
        return blocker

    policy_error = validate_booking_policy(user.student, campus_resource)
    if policy_error:
        return policy_error

    campus_resource.refresh_from_db(fields=["slots_booked", "max_capacity", "end_time"])
    if campus_resource.slots_booked >= campus_resource.max_capacity:
        return _response("CAPACITY_FULL", f"No more slots available for {campus_resource.name}!")

    pass_expiry = datetime.combine(date.today(), campus_resource.end_time)
    generated_pass = NightPass(
        campus_resource=campus_resource,
        pass_id=_generate_pass_id(),
        user=user,
        date=date.today(),
        start_time=now.time(),
        end_time=pass_expiry,
        valid=True,
    )
    generated_pass.save()

    user.student.has_booked = True
    user.student.save(update_fields=["has_booked"])

    campus_resource.slots_booked += 1
    campus_resource.save(update_fields=["slots_booked"])

    return {
        "status": True,
        "reason_code": "PASS_CREATED",
        "pass_qr": None,
        "message": f"Pass generated successfully for {campus_resource.name}!",
    }

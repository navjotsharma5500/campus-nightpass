from django.utils import timezone

from ...global_settings.models import Settings


def _max_violations():
    policy = Settings.current()
    return int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3


def append_violation(user_pass, code, message, occurred_at=None):
    occurred_at = occurred_at or timezone.now()

    codes = [item for item in (user_pass.violation_code or "").split("|") if item]
    if code in codes:
        return False

    codes.append(code)
    user_pass.violation_code = "|".join(codes)

    token = f"[{code}]"
    existing = user_pass.defaulter_remarks or ""
    combined = f"{token} {message}".strip()
    user_pass.defaulter_remarks = f"{existing} | {combined}" if existing else combined
    student = user_pass.user.student
    max_violations = _max_violations()
    became_defaulter = (
        not user_pass.defaulter
        and int(student.violation_flags) < max_violations
        and int(student.violation_flags) + 1 >= max_violations
    )
    user_pass.defaulter = int(student.violation_flags) + 1 >= max_violations
    user_pass.violation_time = occurred_at
    if became_defaulter:
        from ..models import ViolationAuditLog

        ViolationAuditLog.objects.create(
            student=student,
            night_pass=user_pass,
            event_type=ViolationAuditLog.BECAME_DEFAULTER,
            message=f"Student became defaulter: {combined}",
        )
    return True


def violation_codes(user_pass):
    return [item for item in (user_pass.violation_code or "").split("|") if item]


def violation_count(user_pass):
    return len(violation_codes(user_pass))

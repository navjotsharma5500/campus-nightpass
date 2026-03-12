from django.utils import timezone


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
    user_pass.defaulter = True
    user_pass.violation_time = occurred_at
    return True


def violation_codes(user_pass):
    return [item for item in (user_pass.violation_code or "").split("|") if item]


def violation_count(user_pass):
    return len(violation_codes(user_pass))

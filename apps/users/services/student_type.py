"""Canonical student types shared by student import paths."""
from django.core.exceptions import ValidationError


def normalize_student_type(value):
    key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if key in ("HOSTELLER", "HOSTLER"):
        return "HOSTELLER"
    if key == "DAY_SCHOLAR":
        return "DAY_SCHOLAR"
    raise ValidationError("student_type must be HOSTELLER or DAY_SCHOLAR.")


def prepare_student_type_row(row):
    if "student_type" in row:
        row["student_type"] = normalize_student_type(row["student_type"])
        if row["student_type"] == "DAY_SCHOLAR":
            row["hostel"] = ""
            row["room_number"] = ""

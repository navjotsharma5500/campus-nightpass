"""Admin upload/preview/confirmation, separate from the legacy importer."""
import logging
from time import perf_counter

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse

from .services.student_sync import apply_sync, preview_sync, read_upload

logger = logging.getLogger("apps.users.student_sync")
SALT = "student-data-sync-v1"


class SyncOptions(forms.Form):
    clear_all = forms.BooleanField(required=False, label="Clear existing hostel/room assignments before applying this file")
    clear_absent = forms.BooleanField(required=False, label="Clear hostel/room only for students not present in this upload")
    allow_blank_picture = forms.BooleanField(required=False, label="Allow blank picture values to clear existing pictures")

    def clean(self):
        data = super().clean()
        if data.get("clear_all") and data.get("clear_absent"):
            raise forms.ValidationError("Choose only one hostel clearing option.")
        return data

    def sync_kwargs(self):
        return {"clear": "all" if self.cleaned_data.get("clear_all") else "absent" if self.cleaned_data.get("clear_absent") else "none",
                "allow_blank_picture": self.cleaned_data.get("allow_blank_picture", False)}


class SyncUpload(forms.Form):
    file = forms.FileField(label="Student data file (CSV or XLSX)")
    mode = forms.ChoiceField(choices=(("full", "FULL STUDENT SYNC"), ("hostel", "HOSTEL / ROOM SYNC"), ("picture", "PICTURE URL SYNC")))


def can_sync(model_admin, request):
    # Bulk operations bypass ModelAdmin save hooks, so check all creation permissions.
    return (model_admin.has_change_permission(request) and model_admin.has_add_permission(request)
            and request.user.has_perm("users.add_customuser"))


def student_sync_view(model_admin, request):
    if not can_sync(model_admin, request):
        raise PermissionDenied
    started = perf_counter()
    upload_form = SyncUpload()
    options = SyncOptions()
    context = {**model_admin.admin_site.each_context(request), "opts": model_admin.model._meta,
               "title": "Student Data Sync", "upload_form": upload_form,
               "sync_url": reverse("admin:users_student_data_sync"),
               "list_url": reverse("admin:users_student_changelist")}
    if request.method == "POST":
        try:
            token = request.POST.get("payload")
            if request.POST.get("action") == "apply" and not token:
                raise ValidationError("Validate and preview the file before applying it.")
            if token:
                payload = signing.loads(token, salt=SALT, max_age=1800)
                if payload["actor"] != request.user.pk or payload["session"] != request.session.session_key:
                    raise PermissionDenied
                options = SyncOptions(request.POST)
                if not options.is_valid():
                    raise ValidationError(options.errors.as_text())
            else:
                upload_form = SyncUpload(request.POST, request.FILES)
                context["upload_form"] = upload_form
                if not upload_form.is_valid():
                    return TemplateResponse(request, "admin/users/student/sync.html", context)
                upload = upload_form.cleaned_data["file"]
                headers, rows = read_upload(upload)
                payload = {"headers": headers, "rows": rows, "mode": upload_form.cleaned_data["mode"],
                           "filename": upload.name, "actor": request.user.pk, "session": request.session.session_key,
                           "options": {"clear": "none", "allow_blank_picture": False}}
                options = SyncOptions({})
                options.is_valid()
            kwargs = options.sync_kwargs()
            # Changed checkboxes must receive a fresh preview before confirmation.
            if request.POST.get("action") == "apply" and kwargs == payload["options"]:
                counts, duration = apply_sync(payload["headers"], payload["rows"], payload["mode"],
                                             actor=request.user.pk, filename=payload["filename"], **kwargs)
                messages.success(request, (
                    f"Created students: {counts['students_to_create']}. Updated students: {counts['students_to_update']}. "
                    f"Created users: {counts['users_to_create']}. Reused users: {counts['users_to_reuse']}. "
                    f"Hostel assignments updated: {counts['hostel_assignments_updated']}. "
                    f"Assignments cleared: {counts['assignments_to_clear']}. Pictures updated: {counts['pictures_updated']}. "
                    f"Skipped rows: {counts['skipped_rows']}. Processing duration: {duration:.2f}s."
                ))
                return redirect("admin:users_student_data_sync")
            plan = preview_sync(payload["headers"], payload["rows"], payload["mode"], **kwargs)
            payload["options"] = kwargs
            token = signing.dumps(payload, salt=SALT, compress=True)
            # Keep confirmation within Django's default 2.5 MB request limit.
            if len(token) > 1800000:
                raise ValidationError("Confirmation data is too large; split the upload into smaller files.")
            context.update({"plan": plan, "counts": [(key.replace("_", " ").capitalize(), value) for key, value in plan.counts.items()],
                            "errors": plan.errors[:200], "error_count": len(plan.errors), "payload": token,
                            "options": options, "mode": payload["mode"], "filename": payload["filename"],
                            "duration": f"{perf_counter() - started:.2f}"})
            logger.info("Student sync preview admin=%s mode=%s filename=%r total=%s errors=%s duration=%.3fs",
                        request.user.pk, payload["mode"], payload["filename"], len(payload["rows"]), plan.counts["error_rows"], perf_counter() - started)
        except (ValidationError, signing.BadSignature) as exc:
            context["error"] = "; ".join(exc.messages[:200]) if isinstance(exc, ValidationError) else "Preview expired or invalid. Upload the file again."
        except PermissionDenied:
            raise
        except Exception:
            logger.exception("Student sync request failed admin=%s", request.user.pk)
            context["error"] = "Sync failed. No sync changes were committed. Upload again to revalidate; contact the administrator if this persists."
    return TemplateResponse(request, "admin/users/student/sync.html", context)

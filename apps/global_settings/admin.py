from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from admin_extra_buttons.api import ExtraButtonsMixin, button
from admin_extra_buttons.utils import HttpResponseRedirectToReferrer
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse

from ..nightpass.models import CampusResource
from ..users.models import NightPass, Student
from ..users.services.deadline_evaluator import evaluate_active_pass_deadlines
from ..users.services.violation_utils import violation_count
from .models import Settings


class SettingsAdmin(ExtraButtonsMixin, admin.ModelAdmin):
    list_display = ('pk', 'enable_hostel_limits', 'enable_hostel_timers', 'enable_gender_ratio', 'enable_yearwise_limits')

    @button(html_attrs={'style': 'background-color:#88FF88;color:black'})
    def start_booking(self, request):
        CampusResource.objects.all().update(is_booking=True, booking_complete=False)
        self.message_user(request, "Successfully executed: Start booking")
        return HttpResponseRedirectToReferrer(request)

    @button(html_attrs={'style': 'background-color:#fffd8d;color:black'})
    def stop_booking(self, request):
        CampusResource.objects.all().update(is_booking=False, booking_complete=True)
        self.message_user(request, "Successfully executed: Stop booking")
        return HttpResponseRedirectToReferrer(request)

    @button(html_attrs={'style': 'background-color:#DC6C6C;color:black'})
    def reset_nightpass(self, request):
        CampusResource.objects.all().update(slots_booked=0, booking_complete=False, is_booking=False)
        evaluate_active_pass_deadlines()
        NightPass.objects.filter(date=date.today() - timedelta(days=1)).update(valid=False)
        Student.objects.all().update(is_checked_in=True, last_checkout_time=None, hostel_checkin_time=None, hostel_checkout_time=None, has_booked=False)
        self.message_user(request, "Successfully executed: Nightpass reset")
        return HttpResponseRedirectToReferrer(request)

    @button()
    def evaluate_deadlines(self, request):
        summary = evaluate_active_pass_deadlines()
        self.message_user(
            request,
            (
                "Successfully executed: Evaluate pass deadlines "
                f"(expired={summary['expired_passes']}, library_in={summary['missed_library_in']}, "
                f"library_out={summary['missed_library_out']}, hostel_in={summary['missed_hostel_in']})"
            ),
        )
        return HttpResponseRedirectToReferrer(request)

    @button()
    def force_violation_count(self, request):
        students = Student.objects.select_related('user').all()
        for student in students:
            total = 0
            for user_pass in NightPass.objects.filter(user=student.user):
                total += violation_count(user_pass)
            if student.violation_flags != total:
                student.violation_flags = total
                student.save(update_fields=["violation_flags"])
        self.message_user(request, "Successfully executed: Recalculated violation count from unified policy")
        return HttpResponseRedirectToReferrer(request)

    @button(html_attrs={'style': 'background-color:#dbeafe;color:black'})
    def violations(self, request):
        return HttpResponseRedirect(reverse("superuser_violations"))

    @button(html_attrs={'style': 'background-color:#fee2e2;color:black'})
    def defaulters(self, request):
        return HttpResponseRedirect(reverse("superuser_defaulters"))

    @button(
        html_attrs={
            'style': 'background-color:#fde68a;color:black',
        }
    )
    def normalize_specific_date_violations(self, request):
        def render_input():
            context = dict(
                self.admin_site.each_context(request),
                opts=self.model._meta,
                title="Normalize Specific Date Violations",
                action_url=request.path,
            )
            return TemplateResponse(request, "admin/global_settings/normalize_specific_date.html", context)

        raw_date = (
            (request.GET.get("normalize_date") or "")
            or (request.POST.get("normalize_date") or "")
        ).strip()
        if not raw_date:
            referer = request.META.get("HTTP_REFERER") or ""
            if referer:
                raw_date = (parse_qs(urlparse(referer).query).get("normalize_date", [""])[0] or "").strip()
        if not raw_date:
            return render_input()
        parsed = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        if not parsed:
            self.message_user(request, "Invalid date. Use YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, or YYYY/MM/DD.", level=messages.ERROR)
            return render_input()
        target_date = parsed

        with transaction.atomic():
            students = Student.objects.select_for_update().select_related("user")
            updated_students = 0
            for student in students:
                total = 0
                for user_pass in NightPass.objects.filter(user=student.user):
                    total += violation_count(user_pass)
                if student.violation_flags != total:
                    student.violation_flags = total
                    student.save(update_fields=["violation_flags"])
                    updated_students += 1

        self.message_user(
            request,
            f"Normalization applied for {target_date.isoformat()}. Students recalculated: {updated_students}.",
        )
        return HttpResponseRedirectToReferrer(request)


admin.site.register(Settings, SettingsAdmin)


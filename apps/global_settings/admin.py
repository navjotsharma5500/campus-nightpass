from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.db import transaction
from django.db.models import Count
from .models import Settings
from ..nightpass.models import CampusResource
from ..users.models import Student, NightPass
from ..users.management.commands.check_defaulters import check_defaulters
from ..users.management.commands.check_defaulter_no_checkin import check_defaulters_no_checkin
from ..users.services.deadline_evaluator import evaluate_active_pass_deadlines
from datetime import date, timedelta

from admin_extra_buttons.api import ExtraButtonsMixin, button
from admin_extra_buttons.utils import HttpResponseRedirectToReferrer


class SettingsAdmin(ExtraButtonsMixin, admin.ModelAdmin):
    list_display = ('pk','enable_hostel_limits', 'enable_hostel_timers','enable_gender_ratio','enable_yearwise_limits')

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
        CampusResource.objects.all().update(slots_booked=0, booking_complete=False, is_booking = False)
        NightPass.objects.filter(date=date.today()-timedelta(days=1)).update(valid=False)
        Student.objects.all().update(is_checked_in=True, last_checkout_time=None, hostel_checkin_time=None, hostel_checkout_time=None, has_booked=False)
        self.message_user(request, "Successfully executed: Nightpass reset")
        return HttpResponseRedirectToReferrer(request)
    
    @button()
    def check_defaulters(self, request):
        check_defaulters()
        self.message_user(request, "Successfully executed: Check defaulters")
        return HttpResponseRedirectToReferrer(request)
    
    @button()
    def check_defaulters_no_checkin(self, request):
        check_defaulters_no_checkin()
        self.message_user(request, "Successfully executed: Check defaulters without checkin")
        return HttpResponseRedirectToReferrer(request)
    
    @button()
    def force_violation_count(self, request):
        students = Student.objects.all()
        for student in students:
            student.violation_flags=NightPass.objects.filter(user=student.user, defaulter=True).count()
            student.save()
        self.message_user(request, "Successfully executed: Reset violation count")
        return HttpResponseRedirectToReferrer(request)

    @button()
    def evaluate_deadlines(self, request):
        evaluate_active_pass_deadlines()
        self.message_user(request, "Successfully executed: Evaluate pass deadlines")
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
            'onclick': "const d=prompt('Enter date (YYYY-MM-DD). Leave blank for today.','');if(d!==null){window.location=this.href+'?normalize_date='+encodeURIComponent(d);}return false;",
        }
    )
    def normalize_specific_date_violations(self, request):
        raw_date = (request.GET.get("normalize_date") or "").strip()
        try:
            target_date = date.fromisoformat(raw_date) if raw_date else date.today()
        except ValueError:
            target_date = date.today()

        with transaction.atomic():
            rows = list(
                NightPass.objects.filter(date=target_date, defaulter=True)
                .values("user")
                .annotate(date_defaulters=Count("pass_id"))
            )

            updated_students = 0
            for row in rows:
                student = Student.objects.select_for_update().filter(user_id=row["user"]).first()
                if not student:
                    continue
                date_defaulters = int(row["date_defaulters"] or 0)
                if date_defaulters <= 0:
                    continue
                previous_count = NightPass.objects.filter(
                    user_id=row["user"],
                    defaulter=True,
                ).exclude(date=target_date).count()
                normalized_total = previous_count + 1
                if normalized_total != student.violation_flags:
                    student.violation_flags = normalized_total
                    student.save(update_fields=["violation_flags"])
                    updated_students += 1

            updated_passes = NightPass.objects.filter(date=target_date, defaulter=True).update(
                defaulter=False,
                defaulter_remarks="",
                violation_code=None,
                violation_time=None,
            )

        self.message_user(
            request,
            f"Normalization applied for {target_date.isoformat()}. Students normalized: {updated_students}. Defaulter passes cleared: {updated_passes}.",
        )
        return HttpResponseRedirectToReferrer(request)
# Register your models here.
admin.site.register(Settings, SettingsAdmin)

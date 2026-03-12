from datetime import date, datetime
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from ..global_settings.models import Settings as settings
from ..users.models import NightPass
from ..users.services.pass_policy import get_active_pass_for_user, get_slot_cancel_time, has_any_scan_activity
from .models import CampusResource
from .services.booking_service import create_pass_for_student


@login_required
def campus_resources_home(request):
    Settings = settings.current()
    campus_resources = CampusResource.objects.filter(is_display=True)
    user = request.user
    if user.user_type == 'student':
        if not hasattr(user, "student"):
            messages.error(request, "Student profile is missing for this account. Please contact the administrator.")
            return redirect('/logout')
        user_pass = get_active_pass_for_user(user)
        user_incidents = NightPass.objects.filter(user=user, defaulter=True)

        if Settings.enable_hostel_timers:
            frontend_timer = user.student.hostel.frontend_checkin_timer
            backend_timer = user.student.hostel.backend_checkin_timer
        else:
            frontend_timer = Settings.frontend_checkin_timer
            backend_timer = Settings.backend_checkin_timer
        hostel_out_library_timer = Settings.library_timer_for_hostel_out or 30

        transit_timer_minutes = frontend_timer
        if user_pass and user_pass.current_step == 1 and user_pass.pass_type == "OUTSIDE":
            transit_timer_minutes = hostel_out_library_timer
        elif user_pass and user_pass.current_step == 3:
            transit_timer_minutes = backend_timer

        if transit_timer_minutes is None:
            transit_timer_minutes = 30
        announcement = Settings.announcement if Settings.announcement else False
        cancel_deadline = get_slot_cancel_time(Settings)
        return render(
            request,
            'lmao.html',
            {
                'student': user.student,
                'campus_resources': campus_resources,
                'user_pass': user_pass,
                'user_incidents': user_incidents,
                'frontend_checkin_timer': frontend_timer,
                'hostel_out_library_timer': hostel_out_library_timer,
                'backend_checkin_timer': backend_timer,
                'transit_timer_minutes': int(transit_timer_minutes),
                'announcement': announcement,
                'slot_cancel_timer': cancel_deadline,
            },
        )
    elif user.user_type == 'security':
        return redirect('/access')
    elif user.user_type == 'admin':
        return redirect('/access/admin-dashboard')


@csrf_exempt
@login_required
def generate_pass(request, campus_resource):
    user = request.user
    campus_resource = CampusResource.objects.get(name=campus_resource)
    data = create_pass_for_student(user, campus_resource)
    return HttpResponse(json.dumps(data))


@csrf_exempt
@login_required
def cancel_pass(request):
    user = request.user
    user_nightpass = get_active_pass_for_user(user)
    if not user_nightpass:
        data = {
            'status': False,
            'message': "No active pass to cancel!"
        }
        return HttpResponse(json.dumps(data))

    policy = settings.current()
    last_time = timezone.make_aware(
        datetime.combine(date.today(), get_slot_cancel_time(policy)),
        timezone.get_current_timezone(),
    )
    if timezone.now() > last_time:
        data = {
            'status': False,
            'message': f"Cannot cancel pass after {get_slot_cancel_time(policy).strftime('%I:%M %p').lstrip('0')}."
        }
        return HttpResponse(json.dumps(data))

    if has_any_scan_activity(user_nightpass):
        data = {
            'status': False,
            'message': "Cannot cancel pass after utilization."
        }
        return HttpResponse(json.dumps(data))

    campus_resource = user_nightpass.campus_resource
    user_nightpass.delete()
    campus_resource.slots_booked = max(campus_resource.slots_booked - 1, 0)
    campus_resource.save(update_fields=["slots_booked"])
    user.student.has_booked = False
    user.student.save(update_fields=["has_booked"])
    data = {
        'status': True,
        'message': "Pass cancelled successfully!"
    }
    return HttpResponse(json.dumps(data))


def hostel_home(request):
    user = request.user
    if user.user_type == 'security':
        security_profile = getattr(request.user, "security", None)
        if not security_profile or security_profile.scanner_type != "HOSTEL":
            return redirect('/access')
        hostel = security_profile.hostel
        if hostel:
            hostel_passes = NightPass.objects.filter(valid=True, user__student__hostel=hostel) | NightPass.objects.filter(date=date.today(), user__student__hostel=hostel)
        else:
            hostel_passes = NightPass.objects.filter(valid=True) | NightPass.objects.filter(date=date.today())
        return render(request, 'caretaker.html', {'hostel_passes': hostel_passes})
    else:
        return redirect('/access')


def creators_page(request):
    return render(request, "nightpass/creators.html")

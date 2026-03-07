from django.shortcuts import render, HttpResponse, redirect
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from .models import *
from ..users.models import *
from ..global_settings.models import Settings as settings
import random
import string
import json
from datetime import date, time, datetime
import random, string
from .models import *
from ..users.views import *
from datetime import datetime, date, timedelta
from .services.booking_service import create_pass_for_student


def _is_within_booking_window(current_time, start_time, end_time):
    if start_time <= end_time:
        return start_time <= current_time <= end_time
    return current_time >= start_time or current_time <= end_time


def _format_booking_time(value):
    return value.strftime("%I:%M %p").lstrip("0")


@login_required
def campus_resources_home(request):
    Settings = settings.objects.first()
    campus_resources = CampusResource.objects.filter(is_display=True)
    user = request.user
    if user.user_type == 'student':
        user_pass = NightPass.objects.filter(user=user, valid=True).first()
        user_incidents = NightPass.objects.filter(user=user, defaulter=True)
        
        if Settings.enable_hostel_timers:
            frontend_timer = user.student.hostel.frontend_checkin_timer
            backend_timer = user.student.hostel.backend_checkin_timer
        else:
            frontend_timer = Settings.frontend_checkin_timer
            backend_timer = Settings.backend_checkin_timer

        transit_timer_minutes = frontend_timer
        if user_pass and user_pass.current_step == 3:
            transit_timer_minutes = backend_timer

        if transit_timer_minutes is None:
            transit_timer_minutes = 40
        announcement = Settings.announcement if Settings.announcement else False
        return render(
            request,
            'lmao.html',
            {
                'student': user.student,
                'campus_resources': campus_resources,
                'user_pass': user_pass,
                'user_incidents': user_incidents,
                'frontend_checkin_timer': frontend_timer,
                'backend_checkin_timer': backend_timer,
                'transit_timer_minutes': int(transit_timer_minutes),
                'announcement': announcement,
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
    user_nightpass = NightPass.objects.filter(user=user, valid=True).first()
    if not user_nightpass:
        data={
            'status':False,
            'message':f"No pass to cancel!"
        }
        return HttpResponse(json.dumps(data))
    else:
        last_time = timezone.make_aware(datetime.combine(date.today(), time(20,00)), timezone.get_current_timezone())
        if timezone.now() > last_time:
            data = {
                'status':False,
                'message':f"Cannot cancel pass after 8pm."
            }
            return HttpResponse(json.dumps(data))
        else:
            if user_nightpass.hostel_checkout_time or user_nightpass.library_out_time:

                data={
                    'status':False,
                    'message':f"Cannot cancel pass after utilization."
                }
                return HttpResponse(json.dumps(data))
            
            else:
                user_nightpass.delete()
                user_nightpass.campus_resource.slots_booked -= 1
                user_nightpass.campus_resource.save()
                user.student.has_booked = False
                user.student.save()
                data={
                    'status':True,
                    'message':f"Pass cancelled successfully!"
                }
                return HttpResponse(json.dumps(data))


def hostel_home(request):
    user = request.user
    if request.user.is_staff and user.user_type == 'security':
        security_profile = getattr(request.user, "security", None)
        if not security_profile or security_profile.scanner_type != "HOSTEL" or not security_profile.hostel:
            return redirect('/access')
        hostel = security_profile.hostel
        if not hostel:
            return redirect('/access')
        hostel_passes = NightPass.objects.filter(valid=True, user__student__hostel=hostel) | NightPass.objects.filter(date=date.today(), user__student__hostel=hostel).order_by('check_out')
        return render(request, 'caretaker.html', {'hostel_passes':hostel_passes})
    else:
        return redirect('/access')

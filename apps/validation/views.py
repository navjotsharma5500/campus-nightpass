#views.py inside of validation


from django.shortcuts import render, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils import timezone
from django.db.models import Count
from django.db.models.functions import TruncDay, TruncMonth
from datetime import datetime, date, timedelta
import json
import requests
from django.shortcuts import redirect

from ..users.models import NightPass, Student
from ..nightpass.models import CampusResource, Hostel
from ..global_settings.models import Settings
from .services.scan_service import process_scan, scanner_location_label, get_scan_window

TRANSIT_LIMIT_MINUTES = 40


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def json_response(data):
    return HttpResponse(json.dumps(data, default=str), content_type="application/json")


def is_admin(user):
    return user.is_staff or user.is_superuser


# ---------------------------------------------------------
# EXTERNAL LIBRARY API
# ---------------------------------------------------------

def req_library_logs(registration_number):
    req = requests.session()
    try:
        req.post(
            "https://library.thapar.edu/inout/login_verify.php",
            data={"name": "user", "pass": "$#**123", "loc": "TESTLIB", "submit": "Login"},
            verify=False
        )
        req.get(f"https://library.thapar.edu/inout/user.php?id={registration_number}")
    except Exception as e:
        print(f"Library API Error: {e}")
    finally:
        req.close()


# ---------------------------------------------------------
# STEP SYSTEM LOGIC
# ---------------------------------------------------------

def checkout_from_hostel(user_pass):
    if user_pass.current_step != 0:
        return json_response({'status': False, 'message': 'Invalid step for Hostel Exit.'})

    now = timezone.now()
    student = user_pass.user.student

    student.is_checked_in = False
    user_pass.hostel_checkout_time = now
    user_pass.current_step = 1

    student.save()
    user_pass.save()

    return json_response({'status': True, 'message': 'Hostel Exit Authorized.'})


def checkin_to_location(user_pass, campus_resource):
    if user_pass.current_step != 1:
        return json_response({'status': False, 'message': 'Exit hostel first.'})

    now = timezone.now()

    # 15 minute transit check
    if user_pass.hostel_checkout_time:
        transit = now - user_pass.hostel_checkout_time
        if transit > timedelta(minutes=TRANSIT_LIMIT_MINUTES):
            user_pass.defaulter = True
            user_pass.defaulter_remarks = f"Late arrival ({transit.seconds // 60} mins)"
            student = user_pass.user.student
            student.violation_flags += 1
            student.save()

    user_pass.library_in_time = now
    user_pass.current_step = 2
    user_pass.save()

    return json_response({'status': True, 'message': f'Checked into {"Library"}'})


def checkout_from_location(user_pass, campus_resource):
    if user_pass.current_step != 2:
        return json_response({'status': False, 'message': 'Student not inside resource.'})

    now = timezone.now()

    user_pass.library_out_time = now
    user_pass.current_step = 3
    user_pass.save()

    return json_response({'status': True, 'message': 'Resource Exit recorded.'})


def checkin_to_hostel(student):
    user_pass = NightPass.objects.filter(user=student.user, valid=True).first()

    if not user_pass or user_pass.current_step != 3:
        return json_response({'status': False, 'message': 'Must exit resource first.'})

    now = timezone.now()

    if user_pass.library_out_time:
        transit = now - user_pass.library_out_time
        if transit > timedelta(minutes=TRANSIT_LIMIT_MINUTES):
            user_pass.defaulter = True
            remark = f"Late return ({transit.seconds // 60} mins)"
            user_pass.defaulter_remarks = (
                (user_pass.defaulter_remarks + " | " + remark)
                if user_pass.defaulter_remarks else remark
            )
            student.violation_flags += 1

    student.is_checked_in = True
    student.hostel_checkin_time = now
    student.has_booked = False

    user_pass.hostel_checkin_time = now
    user_pass.current_step = 4
    user_pass.valid = False

    student.save()
    user_pass.save()

    return json_response({'status': True, 'message': 'Hostel Entry Success. Pass Closed.'})


# ---------------------------------------------------------
# AUTO SCAN + AUTO CHECKIN / CHECKOUT
# ---------------------------------------------------------
@csrf_exempt
@login_required
def kiosk_extension(request):
    reg_no = request.POST.get('registration_number') or request.GET.get('registration_number')
    result = process_scan(reg_no, request.user)
    return json_response(result)

# ---------------------------------------------------------
# SCANNER PAGE
# ---------------------------------------------------------

@login_required
def scanner(request):

    campus_resources = CampusResource.objects.filter(is_display=True)
    scan_start, scan_end = get_scan_window()
    scan_window_text = f"{scan_start.strftime('%I:%M %p').lstrip('0')} - {scan_end.strftime('%I:%M %p').lstrip('0')}"
    context = {
        'check_in_count': NightPass.objects.filter(
            current_step=2,
            valid=True
        ).count(),

        'total_count': NightPass.objects.filter(
            valid=True
        ).count(),

        'campus_resources': campus_resources,
        'user_incidents': NightPass.objects.filter(
            defaulter=True
        ).order_by('-date')[:5],
        'security_location': scanner_location_label(request.user),
        'scan_window_text': scan_window_text,
    }

    return render(request, 'info.html', context)


# ---------------------------------------------------------
# ADMIN DASHBOARD
# ---------------------------------------------------------


@user_passes_test(is_admin)
def admin_dashboard(request):
    today = date.today()
    policy = Settings.objects.first()

    recent_checkins = NightPass.objects.select_related(
        "user__student", "campus_resource"
    ).order_by("-date")[:12]

    max_violations = int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3

    context = {
        "student_count": Student.objects.count(),
        "active_checkins": NightPass.objects.filter(
            current_step=2,
            valid=True
        ).count(),
        "active_passes": NightPass.objects.filter(valid=True).count(),
        "bookings_today": NightPass.objects.filter(date=today).count(),
        "completed_today": NightPass.objects.filter(date=today, current_step=4).count(),
        "in_transit": NightPass.objects.filter(valid=True, current_step__in=[1, 3]).count(),
        "defaulters": NightPass.objects.filter(date=today, defaulter=True).count(),
        "blocked_students": Student.objects.filter(violation_flags__gte=max_violations).count(),
        "recent_checkins": recent_checkins,
    }

    return render(request, "nightpass/admin_dashboard.html", context)


# ---------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------

@user_passes_test(is_admin)
def analytics(request):

    last_30_days = date.today() - timedelta(days=30)

    daily_data = NightPass.objects.filter(date__gte=last_30_days) \
        .annotate(day=TruncDay('date')) \
        .values('day') \
        .annotate(count=Count('pass_id')) \
        .order_by('day')

    daily_labels = [d['day'].strftime("%d %b") for d in daily_data]
    daily_counts = [d['count'] for d in daily_data]

    monthly_data = NightPass.objects.annotate(month=TruncMonth('date')) \
        .values('month') \
        .annotate(count=Count('pass_id')) \
        .order_by('month')

    monthly_labels = [m['month'].strftime("%B") for m in monthly_data]
    monthly_counts = [m['count'] for m in monthly_data]

    context = {
        'total_students': Student.objects.count(),
        'total_passes': NightPass.objects.count(),
        'active_passes': NightPass.objects.filter(valid=True).count(),
        'completed_passes': NightPass.objects.filter(current_step=4).count(),
        'defaulters': Student.objects.filter(violation_flags__gt=0).count(),
        'daily_labels': daily_labels,
        'daily_counts': daily_counts,
        'monthly_labels': monthly_labels,
        'monthly_counts': monthly_counts,
    }

    return render(request, "nightpass/analytics.html", context)

@user_passes_test(is_admin)
def simple_student_list(request):
    students = Student.objects.all().order_by('registration_number')

    context = {
        "students": students
    }

    return render(request, "nightpass/simple_student_list.html", context)

import openpyxl
from django.http import HttpResponse


import openpyxl
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from openpyxl.styles import Font


@user_passes_test(is_admin)
def download_report_range(request):

    start_date = parse_date(request.GET.get("start_date"))
    end_date = parse_date(request.GET.get("end_date"))

    # Validation
    if not start_date or not end_date:
        return HttpResponse("Invalid date range")

    if end_date < start_date:
        return HttpResponse("End date cannot be before start date")

    passes = NightPass.objects.filter(
        date__range=[start_date, end_date]
    ).select_related("user__student", "user__student__hostel")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "NightPass Report"

    headers = [
        "Student Name",
        "Registration Number",
        "Hostel",
        "Check-out (Hostel)",
        "Check-in (Library)",
        "Check-out (Library)",
        "Return to Hostel",
        "Current Step",
        "Valid",
        "Defaulter",
        "Date"
    ]

    sheet.append(headers)

    # Bold header row
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for p in passes:
        student = p.user.student

        sheet.append([
            student.name,
            student.registration_number,
            student.hostel.name if student.hostel else "",
            p.hostel_checkout_time,
            p.library_in_time,
            p.library_out_time,
            p.hostel_checkin_time,
            p.current_step,
            p.valid,
            p.defaulter,
            p.date
        ])

    # Auto column width
    for column in sheet.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        sheet.column_dimensions[column_letter].width = max_length + 2

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    response["Content-Disposition"] = (
        f'attachment; filename="NightPass_{start_date}_to_{end_date}.xlsx"'
    )

    workbook.save(response)
    return response

from django.http import JsonResponse

@login_required
def get_status_json(request):
    """
    This is the 'heartbeat' for the student's phone. 
    It tells the phone what the current step is in the database.
    """
    
    user_pass = NightPass.objects.filter(user=request.user, valid=True).first()
    
    if user_pass:
        return JsonResponse({'current_step': user_pass.current_step})
    
    # Return -1 if no pass exists (e.g., it was just closed/finished)
    return JsonResponse({'current_step': -1})

from django.shortcuts import render, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils import timezone
from django.db.models import Count
from django.db.models.functions import TruncDay, TruncMonth
from datetime import datetime, date, timedelta
import json
import requests

from ..users.models import NightPass, Student
from ..nightpass.models import CampusResource, Hostel

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
    if not reg_no:
        return json_response({'status': False, 'message': 'Registration number missing.'})

    try:
        # Use select_related to get user and hostel data in one query
        student = Student.objects.select_related('user', 'hostel').get(registration_number=reg_no)
    except Student.DoesNotExist:
        return json_response({'status': False, 'message': 'Student not found.'})

    user_pass = NightPass.objects.filter(user=student.user, valid=True).first()
    if not user_pass:
        return json_response({'status': False, 'message': 'No active pass found for this student.'})

    # Execute the step logic
    if user_pass.current_step == 0:
        response = checkout_from_hostel(user_pass)
    elif user_pass.current_step == 1:
        response = checkin_to_location(user_pass, "Library")
    elif user_pass.current_step == 2:
        response = checkout_from_location(user_pass, "Library")
    elif user_pass.current_step == 3:
        response = checkin_to_hostel(student)
    else:
        return json_response({'status': False, 'message': 'Pass is in an invalid state.'})

    # Convert the logic response to a dict so we can add student info to it
    # This is the "Data Fetching" part your frontend is looking for
    # Convert the logic response to a dict safely
    try:
        result = json.loads(response.content.decode('utf-8'))
    except Exception:
        result = {'status': False, 'message': 'Logic error'}
    
    if result.get('status'):
        # Fix the Picture check
        if student.picture and hasattr(student.picture, 'url'):
            pic_url = student.picture.url
        else:
            pic_url = str(student.picture) if student.picture else "https://static.vecteezy.com/system/resources/previews/005/129/844/non_2x/profile-user-icon-isolated-on-white-background-eps10-free-vector.jpg"

        # Use .pk instead of .id to avoid the AttributeError
        result.update({
            "user": {
                "name": student.name,
                "registration_number": student.registration_number,
                "hostel": student.hostel.name if student.hostel else "N/A",
                "picture": pic_url
            },
            "task": {"check_in": False, "check_out": False},
            "user_pass": {"pass_id": user_pass.pk}  # Changed .id to .pk
        })
    
    return json_response(result)
    security = getattr(request.user, 'security', None)
    if not security:
        return json_response({'status': False, 'message': 'Security profile missing.'})

    # STEP 0 → Leaving Hostel
    if user_pass.current_step == 0:
        checkout_from_hostel(user_pass)

    # STEP 1 → Entering Campus Resource
    elif user_pass.current_step == 1:
        checkin_to_location(user_pass, "Library")

    # STEP 2 → Leaving Campus Resource
    elif user_pass.current_step == 2:
        checkout_from_location(user_pass, "Library")

    # STEP 3 → Returning to Hostel
    elif user_pass.current_step == 3:
        checkin_to_hostel(student)

    # refresh data after step change
    user_pass.refresh_from_db()

    return json_response({
        "status": True,
        "message": "Scan Successful",
        "student_name": student.name,
        "registration_number": student.registration_number,
        "current_step": user_pass.current_step,
        "pass_id": user_pass.id,
        "student_hostel": student.hostel.name if student.hostel else "-"
    })

# ---------------------------------------------------------
# SCANNER PAGE
# ---------------------------------------------------------

@login_required
def scanner(request):

    campus_resources = CampusResource.objects.filter(is_display=True)

    context = {
        'check_in_count': NightPass.objects.filter(
            current_step=2,
            valid=True,
            date=date.today()
        ).count(),

        'total_count': NightPass.objects.filter(
            valid=True,
            date=date.today()
        ).count(),

        'campus_resources': campus_resources,
        'user_incidents': NightPass.objects.filter(
            defaulter=True
        ).order_by('-date')[:5]
    }

    return render(request, 'info.html', context)


# ---------------------------------------------------------
# ADMIN DASHBOARD
# ---------------------------------------------------------


@user_passes_test(is_admin)
def admin_dashboard(request):

    recent_checkins = NightPass.objects.select_related(
        "user__student", "campus_resource"
    ).order_by("-start_time")[:10]

    context = {
        "student_count": Student.objects.count(),

        "active_checkins": NightPass.objects.filter(
            current_step=2,
            valid=True
        ).count(),

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
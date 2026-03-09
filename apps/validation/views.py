#views.py inside of validation


from django.shortcuts import render, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils import timezone
from django.db.models import Count
from django.db.models.functions import TruncDay, TruncMonth
from django.core.paginator import Paginator
from django.utils.dateparse import parse_date
from django.db.models import Q
from datetime import datetime, date, timedelta
import json
import requests
from django.shortcuts import redirect
from collections import OrderedDict

from ..users.models import NightPass, Student
from ..users.services.violation_utils import violation_codes
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
    return user.is_superuser or getattr(user, "user_type", None) == "admin"


def is_scanner(user):
    return getattr(user, "user_type", None) == "security"


def _dashboard_step_label(user_pass):
    mapping = {
        0: "Booked",
        1: "Hostel Out",
        2: "Library IN",
        3: "Library Out",
        4: "Hostel IN",
    }
    return mapping.get(user_pass.current_step, "Booked")


def _format_pass_for_dashboard(user_pass, max_violations):
    student = user_pass.user.student
    if student.violation_flags >= max_violations:
        dashboard_status = "Block"
    elif user_pass.defaulter:
        dashboard_status = "Violation"
    else:
        dashboard_status = _dashboard_step_label(user_pass)

    user_pass.dashboard_status = dashboard_status
    user_pass.violation_codes_display = ", ".join(violation_codes(user_pass)) or "-"
    return user_pass


def _scanner_pass_type_label(user_pass):
    return "Hostel Out" if user_pass.pass_type == "OUTSIDE" else "Library"


def _scanner_status_label(user_pass):
    if user_pass.defaulter:
        return "Violation"

    mapping = {
        0: "Night Pass Approved",
        1: "Hostel OUT",
        2: "Library IN",
        3: "Library OUT",
        4: "Returned to Hostel",
    }
    return mapping.get(user_pass.current_step, "Night Pass Approved")


def _format_pass_for_scanner(user_pass):
    user_pass.pass_type_label = _scanner_pass_type_label(user_pass)
    user_pass.scanner_status = _scanner_status_label(user_pass)
    return user_pass


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
@user_passes_test(is_scanner)
def kiosk_extension(request):
    reg_no = request.POST.get('registration_number') or request.GET.get('registration_number')
    result = process_scan(reg_no, request.user)
    return json_response(result)

# ---------------------------------------------------------
# SCANNER PAGE
# ---------------------------------------------------------

@login_required
@user_passes_test(is_scanner)
def scanner(request):
    today = timezone.localdate()
    campus_resources = CampusResource.objects.filter(is_display=True)
    security_profile = getattr(request.user, "security", None)
    scan_start, scan_end = get_scan_window()
    scan_window_text = f"{scan_start.strftime('%I:%M %p').lstrip('0')} - {scan_end.strftime('%I:%M %p').lstrip('0')}"
    student_passes = NightPass.objects.select_related(
        "user__student",
        "user__student__hostel",
        "campus_resource",
    ).filter(date=today).order_by(
        "user__student__hostel__name",
        "user__student__name",
        "-start_time",
    )

    scanner_view = "library"
    if security_profile and security_profile.scanner_type == "HOSTEL":
        scanner_view = "hostel"
        if security_profile.hostel_id:
            student_passes = student_passes.filter(user__student__hostel_id=security_profile.hostel_id)

    paginator = Paginator(student_passes, 10)
    student_page = paginator.get_page(request.GET.get("page", 1))
    page_passes = [_format_pass_for_scanner(user_pass) for user_pass in student_page.object_list]

    grouped_passes = OrderedDict()
    if scanner_view == "hostel":
        for user_pass in page_passes:
            hostel_name = user_pass.user.student.hostel.name if user_pass.user.student.hostel else "No Hostel Assigned"
            grouped_passes.setdefault(hostel_name, []).append(user_pass)

    context = {
        'check_in_count': NightPass.objects.filter(
            date=today,
            current_step=2,
            valid=True
        ).count(),

        'total_count': NightPass.objects.filter(
            date=today,
            valid=True
        ).count(),

        'campus_resources': campus_resources,
        'user_incidents': NightPass.objects.filter(
            defaulter=True
        ).order_by('-date')[:5],
        'security_location': scanner_location_label(request.user),
        'scan_window_text': scan_window_text,
        'scanner_view': scanner_view,
        'student_page': student_page,
        'student_passes': page_passes,
        'grouped_student_passes': grouped_passes,
    }

    return render(request, 'info.html', context)


# ---------------------------------------------------------
# ADMIN DASHBOARD
# ---------------------------------------------------------


@user_passes_test(is_admin)
def admin_dashboard(request):
    today = timezone.localdate()
    policy = Settings.objects.first()
    max_violations = int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3

    today_passes = NightPass.objects.select_related(
        "user__student", "campus_resource"
    ).filter(date=today)

    activity_tab = request.GET.get("activity", "all")
    activity_qs = today_passes

    if activity_tab == "in_transit":
        activity_qs = activity_qs.filter(current_step__in=[1, 3])
    elif activity_tab == "in_library":
        activity_qs = activity_qs.filter(current_step=2)
    elif activity_tab == "complete":
        activity_qs = activity_qs.filter(current_step=4)
    elif activity_tab == "defaulters":
        activity_qs = activity_qs.filter(
            Q(defaulter=True) | Q(user__student__violation_flags__gte=max_violations)
        )

    activity_qs = activity_qs.order_by(
        "-hostel_checkin_time",
        "-library_out_time",
        "-library_in_time",
        "-hostel_checkout_time",
        "-pass_id",
    )

    paginator = Paginator(activity_qs, 10)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    recent_checkins = [
        _format_pass_for_dashboard(pass_obj, max_violations)
        for pass_obj in page_obj.object_list
    ]

    context = {
        "active_checkins": today_passes.filter(
            current_step=2,
            valid=True
        ).count(),
        "active_passes": today_passes.filter(valid=True).count(),
        "completed_today": today_passes.filter(current_step=4).count(),
        "in_transit": today_passes.filter(valid=True, current_step__in=[1, 3]).count(),
        "violation_count": today_passes.filter(defaulter=True).count(),
        "blocked_students": Student.objects.filter(violation_flags__gte=max_violations).count(),
        "recent_checkins": recent_checkins,
        "page_obj": page_obj,
        "activity_tab": activity_tab,
    }

    return render(request, "nightpass/admin_dashboard.html", context)


# ---------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------

@user_passes_test(is_admin)
def analytics(request):

    today = timezone.localdate()
    default_from = today - timedelta(days=30)
    from_date = parse_date(request.GET.get("from_date", ""))
    to_date = parse_date(request.GET.get("to_date", ""))

    if not from_date or not to_date or from_date > to_date:
        from_date = default_from
        to_date = today

    base_qs = NightPass.objects.filter(date__range=[from_date, to_date])

    daily_data = base_qs \
        .annotate(day=TruncDay('date')) \
        .values('day') \
        .annotate(count=Count('pass_id')) \
        .order_by('day')

    daily_labels = [d['day'].strftime("%d %b") for d in daily_data]
    daily_counts = [d['count'] for d in daily_data]

    monthly_data = base_qs.annotate(month=TruncMonth('date')) \
        .values('month') \
        .annotate(count=Count('pass_id')) \
        .order_by('month')

    monthly_labels = [m['month'].strftime("%B") for m in monthly_data]
    monthly_counts = [m['count'] for m in monthly_data]

    context = {
        'total_students': Student.objects.count(),
        'total_passes': base_qs.count(),
        'active_passes': base_qs.filter(valid=True).count(),
        'completed_passes': base_qs.filter(current_step=4).count(),
        'defaulters': base_qs.filter(defaulter=True).count(),
        'daily_labels': daily_labels,
        'daily_counts': daily_counts,
        'monthly_labels': monthly_labels,
        'monthly_counts': monthly_counts,
        'from_date': from_date.isoformat(),
        'to_date': to_date.isoformat(),
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


@user_passes_test(is_admin)
def dashboard_detail(request, segment):
    today = timezone.localdate()
    policy = Settings.objects.first()
    max_violations = int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3

    title = "Dashboard Details"
    entries = []
    students = []

    passes_qs = NightPass.objects.select_related("user__student", "campus_resource").filter(date=today)

    if segment == "active-checkins":
        title = "Active Check IN"
        entries = passes_qs.filter(valid=True, current_step=2)
    elif segment == "active-passes":
        title = "Active Passes"
        entries = passes_qs.filter(valid=True)
    elif segment == "voilation":
        title = "Violation"
        entries = passes_qs.filter(defaulter=True)
    elif segment == "in-transit":
        title = "IN Transit"
        entries = passes_qs.filter(valid=True, current_step__in=[1, 3])
    elif segment == "completed-today":
        title = "Complete Today"
        entries = passes_qs.filter(current_step=4)
    elif segment == "blocked-students":
        title = "Blocked Students"
        students = Student.objects.select_related("hostel").filter(violation_flags__gte=max_violations).order_by("-violation_flags", "name")

    entries = [
        _format_pass_for_dashboard(entry, max_violations)
        for entry in entries
    ]

    return render(
        request,
        "nightpass/dashboard_detail.html",
        {
            "title": title,
            "segment": segment,
            "entries": entries,
            "students": students,
            "max_violations": max_violations,
        },
    )

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

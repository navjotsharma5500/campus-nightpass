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
import openpyxl
from openpyxl.styles import Font

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


def _parse_dashboard_date(request):
    selected_date = parse_date((request.GET.get("date") or "").strip())
    if selected_date:
        return selected_date
    return timezone.localdate()


def _activity_queryset_for_date(selected_date):
    return NightPass.objects.select_related(
        "user__student",
        "user__student__hostel",
        "campus_resource",
    ).filter(date=selected_date)


def _blocked_students_for_date(selected_date, max_violations):
    return Student.objects.select_related("hostel").filter(
        violation_flags__gte=max_violations,
        user__nightpass__date=selected_date,
    ).distinct().order_by("-violation_flags", "name")


def _apply_activity_filter(queryset, activity_tab, max_violations):
    if activity_tab == "in_transit":
        return queryset.filter(current_step__in=[1, 3])
    if activity_tab == "in_library":
        return queryset.filter(current_step=2)
    if activity_tab == "complete":
        return queryset.filter(current_step=4)
    if activity_tab == "defaulters":
        return queryset.filter(
            Q(defaulter=True) | Q(user__student__violation_flags__gte=max_violations)
        )
    return queryset


def _student_search_queryset(search_term):
    queryset = Student.objects.select_related("user", "hostel")
    if not search_term:
        return queryset.none()
    return queryset.filter(
        Q(registration_number__icontains=search_term)
        | Q(name__icontains=search_term)
        | Q(user__email__icontains=search_term)
    ).order_by("name")


def _resolve_selected_student(search_term, registration_number):
    if registration_number:
        return Student.objects.select_related("user", "hostel").filter(
            registration_number=registration_number
        ).first()

    matches = _student_search_queryset(search_term)
    if matches.count() == 1:
        return matches.first()
    return None


def _student_timeline(student, max_violations):
    records = student.user.nightpass_set.select_related(
        "user__student", "user__student__hostel", "campus_resource"
    ).order_by("-date", "-start_time")
    return [_format_pass_for_dashboard(record, max_violations) for record in records]


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
    selected_date = _parse_dashboard_date(request)
    policy = Settings.objects.first()
    max_violations = int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3

    today_passes = _activity_queryset_for_date(selected_date)

    activity_tab = request.GET.get("activity", "all")
    activity_qs = _apply_activity_filter(today_passes, activity_tab, max_violations)

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

    search_term = (request.GET.get("q") or "").strip()
    student_matches = _student_search_queryset(search_term)[:25] if search_term else []
    selected_student = _resolve_selected_student(search_term, (request.GET.get("student") or "").strip())
    student_timeline = _student_timeline(selected_student, max_violations) if selected_student else []
    blocked_on_selected = (
        selected_student.violation_flags >= max_violations
        if selected_student
        else False
    )

    context = {
        "active_checkins": today_passes.filter(
            current_step=2,
            valid=True
        ).count(),
        "active_passes": today_passes.filter(valid=True).count(),
        "completed_today": today_passes.filter(current_step=4).count(),
        "in_transit": today_passes.filter(valid=True, current_step__in=[1, 3]).count(),
        "violation_count": today_passes.filter(defaulter=True).count(),
        "blocked_students": _blocked_students_for_date(selected_date, max_violations).count(),
        "recent_checkins": recent_checkins,
        "page_obj": page_obj,
        "activity_tab": activity_tab,
        "selected_date": selected_date.isoformat(),
        "selected_date_display": selected_date.strftime("%d %b %Y"),
        "search_term": search_term,
        "student_matches": student_matches,
        "selected_student": selected_student,
        "student_timeline": student_timeline,
        "blocked_on_selected": blocked_on_selected,
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
    students = Student.objects.all().select_related("hostel").order_by('registration_number')

    context = {
        "students": students
    }

    return render(request, "nightpass/simple_student_list.html", context)

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
    selected_date = _parse_dashboard_date(request)
    policy = Settings.objects.first()
    max_violations = int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3

    title = "Dashboard Details"
    entries = []
    students = []

    passes_qs = _activity_queryset_for_date(selected_date)

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
        students = _blocked_students_for_date(selected_date, max_violations)

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
            "selected_date": selected_date.isoformat(),
            "selected_date_display": selected_date.strftime("%d %b %Y"),
        },
    )


@user_passes_test(is_admin)
def download_admin_table_excel(request):
    scope = (request.GET.get("scope") or "").strip()
    policy = Settings.objects.first()
    max_violations = int(policy.max_violation_count) if policy and policy.max_violation_count is not None else 3
    selected_date = _parse_dashboard_date(request)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Admin Data"

    if scope == "activity":
        activity_tab = request.GET.get("activity", "all")
        activity_qs = _apply_activity_filter(
            _activity_queryset_for_date(selected_date),
            activity_tab,
            max_violations,
        ).order_by(
            "-hostel_checkin_time",
            "-library_out_time",
            "-library_in_time",
            "-hostel_checkout_time",
            "-pass_id",
        )
        rows = [_format_pass_for_dashboard(row, max_violations) for row in activity_qs]
        headers = [
            "Student",
            "Hostel",
            "Booking Time",
            "Hostel OUT",
            "Library IN",
            "Library OUT",
            "Hostel IN",
            "Violation Time",
            "Violation Type",
            "Status",
        ]
        sheet.append(headers)
        for row in rows:
            sheet.append([
                row.user.student.name,
                row.user.student.hostel.name if row.user.student.hostel else "-",
                row.start_time.strftime("%H:%M:%S") if row.start_time else "-",
                row.hostel_checkout_time.strftime("%d %b %Y %H:%M:%S") if row.hostel_checkout_time else "-",
                row.library_in_time.strftime("%d %b %Y %H:%M:%S") if row.library_in_time else "-",
                row.library_out_time.strftime("%d %b %Y %H:%M:%S") if row.library_out_time else "-",
                row.hostel_checkin_time.strftime("%d %b %Y %H:%M:%S") if row.hostel_checkin_time else "-",
                row.violation_time.strftime("%d %b %Y %H:%M:%S") if row.violation_time else "-",
                row.violation_codes_display,
                row.dashboard_status,
            ])
        filename = f"activity_{activity_tab}_{selected_date.isoformat()}.xlsx"
    elif scope == "detail":
        segment = (request.GET.get("segment") or "").strip()
        passes_qs = _activity_queryset_for_date(selected_date)
        entries = []
        students = []
        if segment == "active-checkins":
            entries = passes_qs.filter(valid=True, current_step=2)
        elif segment == "active-passes":
            entries = passes_qs.filter(valid=True)
        elif segment == "voilation":
            entries = passes_qs.filter(defaulter=True)
        elif segment == "in-transit":
            entries = passes_qs.filter(valid=True, current_step__in=[1, 3])
        elif segment == "completed-today":
            entries = passes_qs.filter(current_step=4)
        elif segment == "blocked-students":
            students = _blocked_students_for_date(selected_date, max_violations)

        if segment == "blocked-students":
            headers = ["Student", "Registration", "Hostel", "Violations"]
            sheet.append(headers)
            for student in students:
                sheet.append([
                    student.name,
                    student.registration_number,
                    student.hostel.name if student.hostel else "-",
                    student.violation_flags,
                ])
        else:
            rows = [_format_pass_for_dashboard(entry, max_violations) for entry in entries]
            headers = [
                "Student",
                "Hostel",
                "Pass Date",
                "Step",
                "Booking Time",
                "Hostel OUT",
                "Library IN",
                "Library OUT",
                "Hostel IN",
                "Violation Time",
                "Violation Type",
                "Violation",
            ]
            sheet.append(headers)
            for row in rows:
                sheet.append([
                    row.user.student.name,
                    row.user.student.hostel.name if row.user.student.hostel else "-",
                    row.date.strftime("%Y-%m-%d") if row.date else "-",
                    row.dashboard_status,
                    row.start_time.strftime("%H:%M:%S") if row.start_time else "-",
                    row.hostel_checkout_time.strftime("%d %b %Y %H:%M:%S") if row.hostel_checkout_time else "-",
                    row.library_in_time.strftime("%d %b %Y %H:%M:%S") if row.library_in_time else "-",
                    row.library_out_time.strftime("%d %b %Y %H:%M:%S") if row.library_out_time else "-",
                    row.hostel_checkin_time.strftime("%d %b %Y %H:%M:%S") if row.hostel_checkin_time else "-",
                    row.violation_time.strftime("%d %b %Y %H:%M:%S") if row.violation_time else "-",
                    row.violation_codes_display,
                    "Yes" if row.defaulter else "No",
                ])
        filename = f"detail_{segment}_{selected_date.isoformat()}.xlsx"
    elif scope == "student_activity":
        registration_number = (request.GET.get("student") or "").strip()
        student = Student.objects.select_related("user", "hostel").filter(
            registration_number=registration_number
        ).first()
        headers = [
            "Student",
            "Registration",
            "Email",
            "Hostel",
            "Pass Date",
            "Pass Type",
            "Resource",
            "Booking Time",
            "Hostel OUT",
            "Library IN",
            "Library OUT",
            "Hostel IN",
            "Transit Status",
            "Violation Time",
            "Violation Type",
            "Defaulter",
            "Blocked",
            "Valid",
        ]
        sheet.append(headers)
        if student:
            timeline = _student_timeline(student, max_violations)
            is_blocked = student.violation_flags >= max_violations
            for row in timeline:
                transit_status = "In Transit" if row.current_step in [1, 3] and row.valid else row.dashboard_status
                sheet.append([
                    student.name,
                    student.registration_number,
                    student.user.email,
                    student.hostel.name if student.hostel else "-",
                    row.date.strftime("%Y-%m-%d") if row.date else "-",
                    row.pass_type,
                    row.campus_resource.name if row.campus_resource else "-",
                    row.start_time.strftime("%H:%M:%S") if row.start_time else "-",
                    row.hostel_checkout_time.strftime("%d %b %Y %H:%M:%S") if row.hostel_checkout_time else "-",
                    row.library_in_time.strftime("%d %b %Y %H:%M:%S") if row.library_in_time else "-",
                    row.library_out_time.strftime("%d %b %Y %H:%M:%S") if row.library_out_time else "-",
                    row.hostel_checkin_time.strftime("%d %b %Y %H:%M:%S") if row.hostel_checkin_time else "-",
                    transit_status,
                    row.violation_time.strftime("%d %b %Y %H:%M:%S") if row.violation_time else "-",
                    row.violation_codes_display,
                    "Yes" if row.defaulter else "No",
                    "Yes" if is_blocked else "No",
                    "Yes" if row.valid else "No",
                ])
        filename = f"student_activity_{registration_number or 'result'}.xlsx"
    elif scope == "student_search":
        search_term = (request.GET.get("q") or "").strip()
        students = _student_search_queryset(search_term)
        headers = ["Student", "Registration", "Email", "Hostel", "Violations", "Blocked"]
        sheet.append(headers)
        for student in students:
            sheet.append([
                student.name,
                student.registration_number,
                student.user.email if student.user else "-",
                student.hostel.name if student.hostel else "-",
                student.violation_flags,
                "Yes" if student.violation_flags >= max_violations else "No",
            ])
        filename = f"student_search_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    elif scope == "students_all":
        students = Student.objects.select_related("hostel").order_by("registration_number")
        headers = ["Name", "Registration", "Hostel", "Booked", "Status", "Violations"]
        sheet.append(headers)
        for student in students:
            if student.is_checked_in:
                status = "Inside Hostel"
            else:
                status = "Outside (Active Pass)" if student.has_booked else "Outside (No Pass)"
            sheet.append([
                student.name,
                student.registration_number,
                student.hostel.name if student.hostel else "No Hostel",
                "Yes" if student.has_booked else "No",
                status,
                student.violation_flags,
            ])
        filename = "students_all.xlsx"
    else:
        return HttpResponse("Invalid export scope")

    for cell in sheet[1]:
        cell.font = Font(bold=True)

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
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
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

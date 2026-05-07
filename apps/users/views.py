from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.views import View
from django.http import HttpResponse, HttpResponseRedirect
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Q
from django.shortcuts import get_object_or_404
from .models import Student, CustomUser, ViolationAuditLog
from .models import NightPass
from .google_config import config
from django.http import JsonResponse
import requests
from urllib.parse import urlencode
from django.urls import reverse
import json
from .services.violation_utils import violation_codes

DASHBOARD_VIEW_ONLY_GROUP = 'dashboard_view_only'


def get_post_login_redirect(user):
    if getattr(user, 'user_type', None) == 'admin':
        return '/access/admin-dashboard'
    if getattr(user, 'user_type', None) == 'security':
        if user.groups.filter(name=DASHBOARD_VIEW_ONLY_GROUP).exists():
            return '/access/admin-dashboard'
        return '/access'
    return '/'


def is_super_admin(user):
    return user.is_superuser


def _student_search_queryset(queryset, search_term):
    if not search_term:
        return queryset
    return queryset.filter(
        Q(name__icontains=search_term)
        | Q(registration_number__icontains=search_term)
        | Q(email__icontains=search_term)
        | Q(hostel__name__icontains=search_term)
    )


def _students_with_violation_history(queryset):
    return queryset.filter(
        Q(violation_flags__gt=0)
        | Q(user__nightpass__defaulter=True)
        | Q(user__nightpass__violation_code__gt="")
    ).distinct()


def _allow_students(students, admin_user):
    allowed = 0
    for student in students:
        had_active_restriction = student.violation_flags > 0
        NightPass.objects.filter(user=student.user, defaulter=True).update(defaulter=False)
        if student.violation_flags != 0:
            student.violation_flags = 0
            student.save(update_fields=["violation_flags"])
        ViolationAuditLog.objects.create(
            student=student,
            event_type=ViolationAuditLog.ALLOWED_AGAIN,
            message="Student was allowed again. Defaulter set to No and violation block cleared.",
            performed_by=admin_user,
        )
        if had_active_restriction:
            allowed += 1
    return allowed


def gauth(request):
    # Load configuration from JSON file
    print(request.build_absolute_uri('/accounts/google/login/callback/'))
    params = {
        'scope': 'profile email',
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'response_type': 'code',
        'state': 'state_parameter_passthrough_value',
        'redirect_uri': request.build_absolute_uri('/accounts/google/login/callback/'),
        'client_id': config['web']['client_id'],
    }

    auth_uri = config['web']['auth_uri']
    redirect_url = f"{auth_uri}?{urlencode(params)}"

    # Redirect to the constructed URL
    return HttpResponseRedirect(redirect_url)

def get_google_user_info(access_token):
    # Google userinfo endpoint URL
    userinfo_url = "https://www.googleapis.com/oauth2/v1/userinfo"

    # Set up the request headers
    headers = {
        'Authorization': f'Bearer {access_token}',
    }

    # Make a GET request to the userinfo endpoint
    response = requests.get(userinfo_url, headers=headers)

    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Parse and return the user information
        user_info = response.json()
        return user_info
    else:
        # Print the error message if the request fails
        print(f"Failed to fetch user information. Status code: {response.status_code}")
        return None


def oauth_callback(request):
    # Load configuration from JSON file
    # Check if the 'code' parameter is present in the GET request
    if 'code' in request.GET:
        # Read the code from the GET parameters
        code = request.GET['code']
        # Google OAuth2 token endpoint URL
        token_endpoint = config['web']['token_uri']
        # Your client credentials
        client_id = config['web']['client_id']
        client_secret = config['web']['client_secret']
        redirect_uri = request.build_absolute_uri('/accounts/google/login/callback/')

        # Build the POST data
        post_data = {
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }

        # Make a POST request to the token endpoint
        response = requests.post(token_endpoint, data=post_data)

        # Check if the request was successful
        if response.ok:
            # Save the response to a JSON file
            user_info = get_google_user_info(response.json()['access_token'])
            user_email = user_info['email']
            user = CustomUser.objects.filter(email=user_email).first()
            if user and user.has_related_object():
                messages.success(request, 'Logged in successfully.')
                login(request, user=user)
                return HttpResponseRedirect(get_post_login_redirect(user))
            else:
                messages.error(request, 'Please use Thapar ID or contact DOSA office.')
                return HttpResponseRedirect('/')
        else:
            # Handle the case when the token request fails
            messages.error(request, 'Service unavailable. Please try again later')
            return HttpResponseRedirect('/')
    else:
        # Handle the case when 'code' parameter is not present
        return HttpResponse('Error: Authorization code not found in GET parameters.')



@csrf_exempt
def login_user(request):
    if not request.user.is_authenticated:
        if request.method == 'POST':
            return gauth(request)
        else:
            return render(request=request, template_name='index.html')
    else:
        return redirect(get_post_login_redirect(request.user))


def logout_user(request):
    logout(request)
    return redirect('/login')


@csrf_exempt
def check_user(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        if not request.user.is_authenticated:
            email = data.get('email')
            password = data.get('password')
            user = authenticate(request, email=email, password=password)
            if user is not None:
                login(request, user)
            else:
                return JsonResponse({'status': False,
                                     'message':'Invalid credentials'})
        if not request.user.is_superuser:
            return JsonResponse({'status': False,
                                 'message':'You are not authorized to access this page'})
        student = Student.objects.filter(registration_number=data.get('registration_number')).first()
        if student:
            image = student.picture
            response = {
                'status':True,
                'image' : image,
                'uuid': student.user.unique_id,
                'message':'Image found' if image else 'Image not found'
            }
            return JsonResponse(response)
        else:
            return JsonResponse({'status': False,
                                 'message':'Student with the given registration number not found'})
        

@csrf_exempt
def update_user_image(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        if not request.user.is_authenticated:
            email = data.get('email')
            password = data.get('password')
            user = authenticate(request, email=email, password=password)
            if user is not None:
                login(request, user)
            else:
                return JsonResponse({'status': False,
                                     'message':'Invalid credentials'})
        if not request.user.is_superuser:
            return JsonResponse({'status': False,
                                 'message':'You are not authorized to access this page'})

        student = Student.objects.filter(registration_number=data.get('registration_number')).first()
        if student:
            student.picture = data.get('url')
            student.save()
            return JsonResponse({'url':data.get('url')})
        else:
            return JsonResponse({'status': False,
                                 'message':'Student with the given registration number not found'})


@user_passes_test(is_super_admin)
def superuser_violations(request):
    if request.method == "POST":
        selected_students = request.POST.getlist("selected_students")
        students = Student.objects.select_related("user").filter(registration_number__in=selected_students)
        allowed = _allow_students(students, request.user)
        if selected_students:
            messages.success(request, f"Allowed {allowed} selected student(s) for new bookings.")
        else:
            messages.warning(request, "Select at least one student to allow.")
        return redirect(request.get_full_path())

    search_term = (request.GET.get("q") or "").strip()
    students = _students_with_violation_history(Student.objects.select_related("hostel"))
    students = _student_search_queryset(students, search_term).order_by("-violation_flags", "name")
    for student in students:
        student.active_restriction = student.violation_flags > 0
    return render(
        request,
        "admin/superuser_student_list.html",
        {
            "title": "Violations",
            "mode": "violations",
            "students": students,
            "search_term": search_term,
        },
    )


@user_passes_test(is_super_admin)
def superuser_violation_detail(request, registration_number):
    student = get_object_or_404(Student.objects.select_related("hostel"), registration_number=registration_number)
    records = student.user.nightpass_set.filter(
        Q(defaulter=True) | Q(violation_code__gt="")
    ).order_by("-violation_time", "-date", "-start_time")
    for record in records:
        record.violation_codes_display = ", ".join(violation_codes(record)) or "-"
    audit_logs = student.violation_audit_logs.select_related("performed_by").all()
    return render(
        request,
        "admin/superuser_student_detail.html",
        {
            "title": f"Violation History: {student.name}",
            "mode": "violations",
            "student": student,
            "records": records,
            "audit_logs": audit_logs,
            "active_restriction": student.violation_flags > 0,
        },
    )


@user_passes_test(is_super_admin)
def superuser_defaulters(request):
    if request.method == "POST":
        selected_students = request.POST.getlist("selected_students")
        students = Student.objects.select_related("user").filter(registration_number__in=selected_students)
        allowed = _allow_students(students, request.user)
        if selected_students:
            messages.success(request, f"Allowed {allowed} selected student(s) for new bookings.")
        else:
            messages.warning(request, "Select at least one student to allow.")
        return redirect(request.get_full_path())

    search_term = (request.GET.get("q") or "").strip()
    students = Student.objects.select_related("hostel").filter(
        Q(violation_flags__gt=0)
        | Q(user__nightpass__defaulter=True)
        | Q(user__nightpass__violation_code__gt="")
    ).distinct()
    students = _student_search_queryset(students, search_term).order_by("-violation_flags", "name")
    for student in students:
        student.active_restriction = student.violation_flags > 0
    return render(
        request,
        "admin/superuser_student_list.html",
        {
            "title": "Blocked Students",
            "mode": "blocked",
            "students": students,
            "search_term": search_term,
        },
    )


@user_passes_test(is_super_admin)
def superuser_defaulter_detail(request, registration_number):
    student = get_object_or_404(Student.objects.select_related("hostel"), registration_number=registration_number)
    records = student.user.nightpass_set.filter(
        Q(defaulter=True) | Q(violation_code__gt="")
    ).order_by("-violation_time", "-date", "-start_time")
    for record in records:
        record.violation_codes_display = ", ".join(violation_codes(record)) or "-"
    audit_logs = student.violation_audit_logs.select_related("performed_by").all()
    return render(
        request,
        "admin/superuser_student_detail.html",
        {
            "title": f"Blocked Student History: {student.name}",
            "mode": "blocked",
            "student": student,
            "records": records,
            "audit_logs": audit_logs,
            "active_restriction": student.violation_flags > 0,
        },
    )


@user_passes_test(is_super_admin)
def superuser_allow_student_record(request, registration_number):
    if request.method != "POST":
        return redirect("/admin/")

    student = get_object_or_404(Student, registration_number=registration_number)
    _allow_students([student], request.user)
    messages.success(request, f"Allowed {student.name} for new bookings.")

    next_url = request.POST.get("next") or reverse("superuser_violations")
    return redirect(next_url)
        

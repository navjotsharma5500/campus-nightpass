
from django.urls import path
from . import views
from .views import (
    scanner,
    kiosk_extension,
    admin_dashboard,
    analytics,
)

urlpatterns = [

    # ----------------------------
    # MAIN SCANNER PAGE
    # ----------------------------
    path('', scanner, name='scanner'),

    # ----------------------------
    # AUTO SCAN ENDPOINT
    # (Scan → Auto Check-In / Check-Out)
    # ----------------------------
    path(
        'extension/fetchuser/performtask/',
        kiosk_extension,
        name='kiosk_extension'
    ),
    
    # Add this line now:
    path('get_status_json/', views.get_status_json, name='get_status_json'),

    # ----------------------------
    # ADMIN
    # ----------------------------
    path(
        'admin-dashboard/',
        admin_dashboard,
        name='admin_dashboard'
    ),

    path(
        'analytics/',
        analytics,
        name='analytics'
    ),
     path(
        "students/",
        views.simple_student_list,
        name="simple_student_list"
    ),
   path(
    "download-report-range/",
    views.download_report_range,
    name="download_report_range"
    ),
]
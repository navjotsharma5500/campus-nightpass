from django.urls import path
from .views import *

urlpatterns = [
    path('login/', login_user),
    path('logout/', logout_user),

    path('accounts/google/login/', gauth),
    path('accounts/google/login/callback/', oauth_callback),
    path('superuser/violations/', superuser_violations, name='superuser_violations'),
    path('superuser/violations/<str:registration_number>/', superuser_violation_detail, name='superuser_violation_detail'),
    path('superuser/defaulters/', superuser_defaulters, name='superuser_defaulters'),
    path('superuser/defaulters/<str:registration_number>/', superuser_defaulter_detail, name='superuser_defaulter_detail'),
    path('superuser/students/<str:registration_number>/allow-record/', superuser_allow_student_record, name='superuser_allow_student_record'),
]

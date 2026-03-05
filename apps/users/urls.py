from django.urls import path
from .views import *

urlpatterns = [
    path('login/', login_user),
    path('logout/', logout_user),

    path('accounts/google/login/', gauth),
    path('accounts/google/login/callback/', oauth_callback),
]

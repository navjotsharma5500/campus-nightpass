from django.urls import path
from . import views

urlpatterns = [
    path('', views.campus_resources_home, name="home"),
    path('book/<str:campus_resource>', views.generate_pass, name="generate_pass"),
    path('cancel/', views.cancel_pass, name="cancel_pass"),
    path('hostel/', views.hostel_home, name="hostel_home"),
    path('creators/', views.creators_page, name='creators_page'),
    ]

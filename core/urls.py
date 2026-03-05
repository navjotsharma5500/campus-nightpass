from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('', include("apps.users.urls")),
    path('', include("apps.nightpass.urls")),
    path('access/', include("apps.validation.urls")),
    path('admin/', admin.site.urls),
    path('hijack/', include('hijack.urls')),
]
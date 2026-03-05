from django.contrib import admin
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from .models import CampusResource, Hostel  # Best practice: avoid * imports

# 1. Define the Resource
class HostelResource(resources.ModelResource):
    class Meta:
        model = Hostel
        # This fixes the 'id' error by using 'name' as the unique key
        import_id_fields = ('name',) 
        # IMPORTANT: Include ALL fields shown as 'Required' in your screenshot
        fields = ('name', 'contact_number', 'email', 'frontend_checkin_timer', 'backend_checkin_timer', 'max_students_allowed')

# 2. Link the Resource to the Admin
class HostelAdmin(ImportExportModelAdmin):
    resource_class = HostelResource  # <--- THIS WAS MISSING
    list_display = ('name', 'contact_number', 'email')
    search_fields = ('name',)

# 3. Repeat for CampusResource if you plan to import those too
class CampusResourceAdmin(ImportExportModelAdmin):
    list_display = ('name', 'max_capacity', 'slots_booked', 'is_booking', 'is_display', 'booking_complete')
    search_fields = ('name',)

admin.site.register(CampusResource, CampusResourceAdmin)
admin.site.register(Hostel, HostelAdmin)
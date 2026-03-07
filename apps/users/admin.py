from django.contrib import admin
from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from rangefilter.filters import DateRangeFilter
from import_export.admin import ImportExportModelAdmin
from import_export import resources
from django.contrib.auth import get_user_model
from datetime import date
from xlsxwriter import Workbook
import io
from import_export import resources, fields # Add 'fields' to imports
from import_export.widgets import ForeignKeyWidget
from apps.nightpass.models import Hostel

from .models import Student, NightPass, Security, Admin, CustomUser

User = get_user_model()
admin.site.index_template = "admin/index.html"


# ==============================
# FILTERS
# ==============================

class YearWiseFilter(admin.SimpleListFilter):
    title = 'Year'
    parameter_name = 'year'

    def lookups(self, request, model_admin):
        return (
            ('1', '1st Year'),
            ('2', '2nd Year'),
            ('3', '3rd Year'),
            ('4', '4th Year'),
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(year=self.value())
        return queryset


# ==============================
# NIGHT PASS ADMIN
# ==============================

class NightPassAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'user',
        'hostel',
        'date',
        'campus_resource',
        'current_step',
        'defaulter'
    )

    search_fields = (
        'user__student__name',
        'user__student__registration_number',
        'user__email'
    )

    list_filter = (
        ('date', DateRangeFilter),
        'campus_resource',
        'user__student__gender',
        'user__student__hostel',
        YearWiseFilter,
        'defaulter',
        'current_step'
    )

    autocomplete_fields = ('user', 'campus_resource')

    readonly_fields = (
        'pass_id',
        'hostel_checkout_time',
        'library_in_time',
        'library_out_time',
        'hostel_checkin_time',
        'current_step'
    )

    def name(self, obj):
        return obj.user.student.name if hasattr(obj.user, "student") else "-"

    def hostel(self, obj):
        if hasattr(obj.user, "student") and obj.user.student.hostel:
            return obj.user.student.hostel.name
        return "-"

    hostel.short_description = "Hostel"

    # ---------------- Export XLSX ---------------- #

    def export_as_xlsx(self, request, queryset):

        headers = [
            'Name', 'Email', 'Hostel', 'Gender', 'Pass ID',
            'Date', 'Resource', 'Step',
            'Hostel Out', 'Library In', 'Library Out', 'Hostel In',
            'Defaulter', 'Remarks'
        ]

        output = io.BytesIO()
        wb = Workbook(output, {'in_memory': True, 'remove_timezone': True})
        ws = wb.add_worksheet()

        for col_num, header in enumerate(headers):
            ws.write(0, col_num, header)

        for row_num, obj in enumerate(queryset, start=1):

            student = obj.user.student if hasattr(obj.user, "student") else None

            row = [
                student.name if student else "-",
                obj.user.email,
                student.hostel.name if student and student.hostel else "-",
                student.gender if student else "-",
                obj.pass_id,
                obj.date.strftime('%d/%m/%y'),
                obj.campus_resource.name,
                f"Step {obj.current_step}",
                timezone.localtime(obj.hostel_checkout_time).strftime('%H:%M:%S') if obj.hostel_checkout_time else "N/A",
                timezone.localtime(obj.library_in_time).strftime('%H:%M:%S') if obj.library_in_time else "N/A",
                timezone.localtime(obj.library_out_time).strftime('%H:%M:%S') if obj.library_out_time else "N/A",
                timezone.localtime(obj.hostel_checkin_time).strftime('%H:%M:%S') if obj.hostel_checkin_time else "N/A",
                "Yes" if obj.defaulter else "No",
                obj.defaulter_remarks or ""
            ]

            for col_num, cell_value in enumerate(row):
                ws.write(row_num, col_num, cell_value)

        wb.close()
        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="nightpass_{date.today()}.xlsx"'
        return response

    export_as_xlsx.short_description = "Export Selected as XLSX"
    actions = ['export_as_xlsx']


# ==============================
# STUDENT IMPORT RESOURCE
# ==============================

class StudentResource(resources.ModelResource):

    hostel = fields.Field(
        column_name='hostel',
        attribute='hostel',
        widget=ForeignKeyWidget(Hostel, 'name') 
    )

    class Meta:


        model = Student
        import_id_fields = ('registration_number',)
        fields = (
            "registration_number",
            "name",
            "hostel",
            "gender",
            "room_number",
            "contact_number",
            "email",
            "parent_contact",
            "year",
            "user",
            "picture"
        )

    def before_import_row(self, row, **kwargs):
        email = row.get("email")

        if not email:
            raise ValueError("Email is required to create user")

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "user_type": "student"
            }
        )

        row["user"] = user.pk


# ==============================
# STUDENT ADMIN
# ==============================

class StudentAdmin(ImportExportModelAdmin):
    resource_class = StudentResource

    list_display = (
        'name',
        'registration_number',
        'hostel',
        'has_booked',
        'current_location',
        'violation_flags'
    )

    search_fields = ('name', 'registration_number')

    autocomplete_fields = ('user',)

    readonly_fields = ('last_checkout_time',)

    list_filter = ('hostel', YearWiseFilter, 'has_booked', 'violation_flags')

    def current_location(self, obj):

        if obj.is_checked_in:
            if obj.hostel:
                return format_html(
                    "<b style='color:green;'>Inside Hostel ({})</b>",
                    obj.hostel.name
                )
            return format_html("<b style='color:red;'>{}</b>", "No Hostel Assigned")

        active_pass = NightPass.objects.filter(
            user=obj.user,
            valid=True
        ).first()

        if active_pass:
            if active_pass.current_step == 2:
                return format_html("<b style='color:blue;'>In {}</b>",
                                   active_pass.campus_resource.name)
            elif active_pass.current_step in [1, 3]:
                return format_html("<b style='color:orange;'>In Transit</b>")

        return format_html("<b style='color:red;'>Outside</b>")

    current_location.short_description = "Status"


# ==============================
# OTHER ADMINS
# ==============================

class SecurityAdmin(admin.ModelAdmin):
    list_display = ('name', 'scanner_type', 'hostel', 'admin_incharge', 'user')
    list_filter = ('scanner_type', 'hostel', 'admin_incharge')
    autocomplete_fields = ('user',)
    ordering = ('user__email',)
    fields = ('name', 'scanner_type', 'hostel', 'admin_incharge', 'user')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.filter(user__user_type='security')

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'user':
            kwargs['queryset'] = CustomUser.objects.filter(user_type='security')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class AdminAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'designation', 'department', "staff_id")
    autocomplete_fields = ('user',)


class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('email', 'user_type')
    search_fields = ('email',)


# ==============================
# REGISTER
# ==============================

admin.site.register(Admin, AdminAdmin)
admin.site.register(Student, StudentAdmin)
admin.site.register(Security, SecurityAdmin)
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(NightPass, NightPassAdmin)

admin.site.site_header = "Thapar NightPass"

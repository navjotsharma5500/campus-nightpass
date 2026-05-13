from django.contrib import admin
from django.contrib import messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib.auth import login
from django import forms
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from rangefilter.filters import DateRangeFilter
from import_export.admin import ImportExportModelAdmin
from import_export import resources
from django.contrib.auth import get_user_model
from datetime import date
from xlsxwriter import Workbook
import io
import logging
from import_export import resources, fields # Add 'fields' to imports
from import_export.results import RowResult
from import_export.widgets import ForeignKeyWidget
from apps.nightpass.models import Hostel

from .models import Student, NightPass, Security, Admin, CustomUser

User = get_user_model()
logger = logging.getLogger(__name__)
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

    class Media:
        css = {"all": ("admin/custom_admin_dashboard.css",)}
        js = ("admin/filter_toggle.js",)


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
        registration_number = str(row.get("registration_number") or "").strip()
        email = str(row.get("email") or "").strip()
        row["registration_number"] = registration_number
        row["email"] = email

        if not registration_number:
            logger.error("Failed row: registration_number is required | row=%s", dict(row))
            raise ValueError("registration_number is required")
        if not email:
            logger.error("Failed row %s: email is required", registration_number)
            raise ValueError("Email is required to create user")

        existing_student = Student.objects.select_related("user").filter(
            registration_number=registration_number
        ).first()

        if existing_student:
            user = existing_student.user
            email_owner = User.objects.filter(email=email).exclude(pk=user.pk).first()
            if email_owner:
                logger.error(
                    "Failed row %s: email %s is already linked to another user",
                    registration_number,
                    email,
                )
                raise ValueError(f"Email {email} is already linked to another user")
            if user.email != email:
                user.email = email
            if user.user_type != "student":
                user.user_type = "student"
            user.save(update_fields=["email", "user_type", "is_staff", "is_superuser"])
        else:
            user, _ = User.objects.get_or_create(
                email=email,
                defaults={
                    "user_type": "student",
                    "is_active": True,
                }
            )
            linked_student = getattr(user, "student", None)
            if linked_student and linked_student.registration_number != registration_number:
                logger.error(
                    "Failed row %s: email %s is already linked to student %s",
                    registration_number,
                    email,
                    linked_student.registration_number,
                )
                raise ValueError(
                    f"Email {email} is already linked to student {linked_student.registration_number}"
                )
            if user.user_type != "student":
                user.user_type = "student"
                user.save(update_fields=["user_type", "is_staff", "is_superuser"])

        row["user"] = user.pk

    def do_instance_save(self, instance, is_create):
        defaults = {
            field.name: getattr(instance, field.name)
            for field in self._meta.model._meta.fields
            if field.name not in ("registration_number",)
        }
        Student.objects.update_or_create(
            registration_number=instance.registration_number,
            defaults=defaults,
        )

    def after_import_row(self, row, row_result, **kwargs):
        registration_number = row.get("registration_number")
        if row_result.import_type == RowResult.IMPORT_TYPE_NEW:
            logger.info("Created student %s", registration_number)
        elif row_result.import_type == RowResult.IMPORT_TYPE_UPDATE:
            logger.info("Updated student %s", registration_number)
        elif row_result.import_type in (RowResult.IMPORT_TYPE_ERROR, RowResult.IMPORT_TYPE_INVALID):
            logger.error("Failed row %s: %s", registration_number, row_result.errors)
        return super().after_import_row(row, row_result, **kwargs)


# ==============================
# STUDENT ADMIN
# ==============================

class StudentAdmin(ImportExportModelAdmin):
    resource_class = StudentResource
    class StudentAdminForm(forms.ModelForm):
        user = forms.ModelChoiceField(
            queryset=CustomUser.objects.filter(user_type='student'),
            required=False,
        )

        class Meta:
            model = Student
            fields = "__all__"

        def clean(self):
            cleaned_data = super().clean()
            if not cleaned_data.get("user") and not cleaned_data.get("email"):
                raise forms.ValidationError("Provide either a linked student user or an email address.")
            return cleaned_data

    form = StudentAdminForm

    list_display = (
        'name',
        'registration_number',
        'hostel',
        'has_booked',
        'hostel_out_status',
        'library_in_status',
        'library_out_status',
        'hostel_in_status',
        'current_location',
        'violation_flags',
        'impersonate_action',
    )

    search_fields = ('name', 'registration_number')

    autocomplete_fields = ('user',)

    readonly_fields = ('last_checkout_time',)

    list_filter = ('hostel', YearWiseFilter, 'has_booked', 'violation_flags')

    def get_urls(self):
        custom = [
            path(
                "impersonate/<str:registration_number>/",
                self.admin_site.admin_view(self.impersonate_student),
                name="student_impersonate",
            )
        ]
        return custom + super().get_urls()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'user':
            kwargs['queryset'] = CustomUser.objects.filter(user_type='student')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        linked_user = form.cleaned_data.get("user")
        email = (form.cleaned_data.get("email") or "").strip()

        if linked_user is None:
            linked_user, _ = CustomUser.objects.get_or_create(
                email=email,
                defaults={"user_type": "student", "is_active": True},
            )

        if linked_user.user_type != "student":
            linked_user.user_type = "student"
            linked_user.save(update_fields=["user_type", "is_staff", "is_superuser"])

        obj.user = linked_user
        if not obj.email:
            obj.email = linked_user.email

        super().save_model(request, obj, form, change)

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

    def _latest_student_pass(self, obj):
        return NightPass.objects.filter(user=obj.user).order_by("-date", "-start_time").first()

    def _tick_cross(self, value):
        return format_html("<span style='color:{};font-weight:700;'>{}</span>", "#16a34a" if value else "#dc2626", "✓" if value else "✗")

    def hostel_out_status(self, obj):
        user_pass = self._latest_student_pass(obj)
        return self._tick_cross(bool(user_pass and user_pass.hostel_checkout_time))

    def library_in_status(self, obj):
        user_pass = self._latest_student_pass(obj)
        return self._tick_cross(bool(user_pass and user_pass.library_in_time))

    def library_out_status(self, obj):
        user_pass = self._latest_student_pass(obj)
        return self._tick_cross(bool(user_pass and user_pass.library_out_time))

    def hostel_in_status(self, obj):
        user_pass = self._latest_student_pass(obj)
        return self._tick_cross(bool(user_pass and user_pass.hostel_checkin_time))

    hostel_out_status.short_description = "Hostel OUT"
    library_in_status.short_description = "Library IN"
    library_out_status.short_description = "Library OUT"
    hostel_in_status.short_description = "Hostel IN"

    def impersonate_action(self, obj):
        if not obj.user_id:
            return "-"
        url = reverse("admin:student_impersonate", args=[obj.registration_number])
        return format_html("<a class='button' href='{}'>Impersonate</a>", url)

    impersonate_action.short_description = "Impersonate"

    def impersonate_student(self, request, registration_number):
        if not request.user.is_superuser:
            self.message_user(request, "Only super admin can impersonate.", level=messages.ERROR)
            return redirect("admin:users_student_changelist")

        student = get_object_or_404(Student.objects.select_related("user"), registration_number=registration_number)
        if not student.user_id:
            self.message_user(request, "Student has no linked user account.", level=messages.ERROR)
            return redirect("admin:users_student_changelist")

        login(request, student.user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("/")

    class Media:
        css = {"all": ("admin/custom_admin_dashboard.css",)}
        js = ("admin/filter_toggle.js",)


# ==============================
# OTHER ADMINS
# ==============================

class CustomUserCreationForm(UserCreationForm):
    scanner_type = forms.ChoiceField(
        choices=Security.SCANNER_TYPE_CHOICES,
        required=False,
        initial=Security.SCANNER_LIBRARY,
        help_text="Used only when creating a security user.",
    )
    hostel = forms.ModelChoiceField(
        queryset=Hostel.objects.all(),
        required=False,
        help_text="Leave blank for a universal hostel scanner.",
    )

    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ("email", "user_type")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user_type"].choices = [
            choice for choice in CustomUser.choices if choice[0] != "student"
        ]

    def clean_user_type(self):
        user_type = self.cleaned_data["user_type"]
        if user_type == "student":
            raise forms.ValidationError("Create students from the Students section, not the Users section.")
        return user_type


class CustomUserChangeForm(UserChangeForm):
    scanner_type = forms.ChoiceField(
        choices=Security.SCANNER_TYPE_CHOICES,
        required=False,
        help_text="Used only for security users.",
    )
    hostel = forms.ModelChoiceField(
        queryset=Hostel.objects.all(),
        required=False,
        help_text="Leave blank for a universal hostel scanner.",
    )

    class Meta:
        model = CustomUser
        fields = ("email", "user_type", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        security_profile = getattr(self.instance, "security", None) if self.instance.pk else None
        if security_profile:
            self.fields["scanner_type"].initial = security_profile.scanner_type
            self.fields["hostel"].initial = security_profile.hostel
        else:
            self.fields["scanner_type"].initial = Security.SCANNER_LIBRARY

    def clean_user_type(self):
        user_type = self.cleaned_data["user_type"]
        if self.instance.pk and self.instance.user_type != "student":
            return user_type
        if user_type == "student":
            raise forms.ValidationError("Student accounts must be managed from the Students section so the student profile is created.")
        return user_type


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

    class Media:
        css = {"all": ("admin/custom_admin_dashboard.css",)}
        js = ("admin/filter_toggle.js",)


class AdminAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'designation', 'department', "staff_id")
    autocomplete_fields = ('user',)

    class Media:
        css = {"all": ("admin/custom_admin_dashboard.css",)}
        js = ("admin/filter_toggle.js",)


class CustomUserAdmin(DjangoUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    list_display = ('email', 'user_type', 'is_active', 'is_staff')
    search_fields = ('email',)
    ordering = ('email',)
    list_filter = ('user_type', 'is_active', 'is_staff')
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Permissions', {'fields': ('user_type', 'is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login',)}),
    )
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('email', 'user_type', 'scanner_type', 'hostel', 'password1', 'password2', 'is_active'),
            },
        ),
    )
    filter_horizontal = ('groups', 'user_permissions')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.exclude(user_type='student')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        if obj.user_type != "security":
            return

        scanner_type = form.cleaned_data.get("scanner_type") or Security.SCANNER_LIBRARY
        hostel = form.cleaned_data.get("hostel") if scanner_type == Security.SCANNER_HOSTEL else None

        Security.objects.update_or_create(
            user=obj,
            defaults={
                "name": getattr(obj, "security", None).name if hasattr(obj, "security") else obj.email,
                "scanner_type": scanner_type,
                "hostel": hostel,
            },
        )

    class Media:
        css = {"all": ("admin/custom_admin_dashboard.css",)}
        js = ("admin/filter_toggle.js",)


# ==============================
# REGISTER
# ==============================

admin.site.register(Admin, AdminAdmin)
admin.site.register(Student, StudentAdmin)
admin.site.register(Security, SecurityAdmin)
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(NightPass, NightPassAdmin)

admin.site.site_header = "Thapar NightPass"

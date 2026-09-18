from django.contrib import admin
from django.contrib import messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib.auth import login
from django import forms
from django.db import connection, transaction
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

def _change_student_registration_number(student, new_registration_number):
    old_registration_number = str(student.pk)
    new_registration_number = str(new_registration_number or "").strip()

    if (
        not new_registration_number
        or new_registration_number == old_registration_number
    ):
        return old_registration_number

    if Student.objects.filter(pk=new_registration_number).exists():
        raise ValueError(
            f"Registration number {new_registration_number} already exists."
        )

    with transaction.atomic():
        # Student.registration_number is the primary key. SQLite does not use
        # ON UPDATE CASCADE for Django foreign keys, so defer FK checks while
        # the Student PK and all direct reverse FK references move together.
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA defer_foreign_keys = ON")

        for relation in Student._meta.related_objects:
            if not (relation.one_to_many or relation.one_to_one):
                continue

            field = relation.field

            if field.target_field != Student._meta.pk:
                continue

            relation.related_model._base_manager.filter(
                **{field.attname: old_registration_number}
            ).update(
                **{field.attname: new_registration_number}
            )

        updated = Student.objects.filter(
            pk=old_registration_number
        ).update(
            registration_number=new_registration_number
        )

        if updated != 1:
            raise RuntimeError(
                "Student registration number update did not affect exactly one row."
            )

        connection.check_constraints()

    student.registration_number = new_registration_number
    return old_registration_number


class StudentAdmin(ImportExportModelAdmin):
    change_list_template = "admin/users/student/change_list.html"

    def changelist_view(self, request, extra_context=None):
        from .student_sync_admin import can_sync
        return super().changelist_view(request, {**(extra_context or {}), "can_student_sync": can_sync(self, request)})

    def student_data_sync(self, request):
        from .student_sync_admin import student_sync_view
        return student_sync_view(self, request)

    resource_class = StudentResource
    class StudentAdminForm(forms.ModelForm):
        email = forms.EmailField(
            required=True,
            help_text=(
                "This is the student's login email. Changing it changes the "
                "email used to sign in while keeping the same account and history."
            ),
        )

        new_registration_number = forms.CharField(
            required=False,
            max_length=20,
            label="Change registration number to",
            help_text=(
                "Only enter a value when correcting the student's roll number. "
                "Leave blank to keep the current registration number."
            ),
        )

        class Meta:
            model = Student
            exclude = ("user",)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

            # New students already have the normal registration_number field.
            # This correction field is only needed when editing an existing student.
            if not self.instance or not self.instance.pk:
                self.fields.pop("new_registration_number", None)

        def clean_new_registration_number(self):
            new_registration_number = (
                self.cleaned_data.get("new_registration_number") or ""
            ).strip()

            if not new_registration_number:
                return ""

            if (
                self.instance
                and self.instance.pk
                and new_registration_number == str(self.instance.pk)
            ):
                return ""

            if Student.objects.filter(
                registration_number=new_registration_number
            ).exists():
                raise forms.ValidationError(
                    "A student with this registration number already exists."
                )

            return new_registration_number

        def clean_email(self):
            email = (self.cleaned_data.get("email") or "").strip().lower()
            if not email:
                raise forms.ValidationError("Email is required.")

            current_user_id = (
                self.instance.user_id
                if self.instance and self.instance.pk
                else None
            )

            if current_user_id:
                other_user = (
                    CustomUser.objects
                    .filter(email__iexact=email)
                    .exclude(pk=current_user_id)
                    .first()
                )
                if other_user:
                    linked_student = Student.objects.filter(
                        user=other_user
                    ).first()
                    if linked_student:
                        raise forms.ValidationError(
                            "This email is already linked to student "
                            f"{linked_student.registration_number}."
                        )
                    raise forms.ValidationError(
                        "This email is already used by another account."
                    )
                return email

            existing_user = CustomUser.objects.filter(
                email__iexact=email
            ).first()

            if existing_user:
                if existing_user.user_type != "student":
                    raise forms.ValidationError(
                        "This email belongs to a non-student account."
                    )

                linked_student = Student.objects.filter(
                    user=existing_user
                ).first()
                if linked_student:
                    raise forms.ValidationError(
                        "This email is already linked to student "
                        f"{linked_student.registration_number}."
                    )

            return email

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

    search_fields = (
        'name',
        'registration_number',
        'email',
        'user__email',
    )

    autocomplete_fields = ()

    readonly_fields = ('last_checkout_time',)

    list_filter = ('hostel', YearWiseFilter, 'has_booked', 'violation_flags')

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        # registration_number is the Student primary key. Normal ModelForm
        # editing can create a second Student row using the same user_id.
        if obj and "registration_number" not in fields:
            fields.append("registration_number")

        return tuple(fields)

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))

        # new_registration_number is removed from the form for new students
        # (StudentAdminForm.__init__), so it must also be excluded from the
        # admin's auto-generated fieldset here, or Add Student raises KeyError.
        if obj is None and "new_registration_number" in fields:
            fields.remove("new_registration_number")

        return fields

    def get_urls(self):
        custom = [
            path("data-sync/", self.admin_site.admin_view(self.student_data_sync), name="users_student_data_sync"),
            path(
                "impersonate/<str:registration_number>/",
                self.admin_site.admin_view(self.impersonate_student),
                name="student_impersonate",
            )
        ]
        return custom + super().get_urls()

    def save_model(self, request, obj, form, change):
        email = form.cleaned_data["email"]
        requested_registration_number = form.cleaned_data.get(
            "new_registration_number"
        )

        if change and obj.user_id:
            # Keep the same CustomUser/User ID and only change login email.
            linked_user = CustomUser.objects.get(pk=obj.user_id)

            if linked_user.email != email:
                linked_user.email = email
                linked_user.save(update_fields=["email"])
        else:
            linked_user = CustomUser.objects.filter(
                email__iexact=email
            ).first()

            if linked_user is None:
                linked_user = CustomUser.objects.create_user(
                    email=email,
                    password=None,
                    user_type="student",
                    is_active=True,
                )

        obj.user = linked_user
        obj.email = email

        super().save_model(request, obj, form, change)

        if change and requested_registration_number:
            old_registration_number = obj.registration_number

            _change_student_registration_number(
                obj,
                requested_registration_number,
            )

            messages.success(
                request,
                "Registration number changed from "
                f"{old_registration_number} to "
                f"{obj.registration_number}.",
            )

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
        return redirect("home")

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

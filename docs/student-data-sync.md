# Student Data Sync

Student admin → **Student Data Sync**. Existing import/export and impersonation remain available.

CampusConnect URL: `https://campusconnect.thapar.edu/permissions/admin/users/student/data-sync/`

Root deployment URL: `/admin/users/student/data-sync/`.

Requires staff admin access, `users.change_student`, `users.add_student`, and
`users.add_customuser` permissions (superusers already have these).

## Workflow and formats

Upload a UTF-8 CSV (BOM accepted) or XLSX, select a mode, and validate. Preview
performs database reads only, reports counts and row errors, and blocks apply if
any row fails. Up to 200 error messages are displayed; correct the file and repeat
validation for the rest. Blank physical rows are ignored. Limits: 20,000 data rows,
10 MB input and 1.8 MB compressed signed confirmation data. Split larger files.

Choose mode-specific options on the preview page and update the preview before
confirming. If options change when Confirm Apply is clicked, the page shows a new
preview and requires confirmation again. Apply revalidates current database state
inside `transaction.atomic()` before any writes; errors roll back the entire sync.
Counts may change if another administrator changes data between preview and apply.

Preview data is compressed and signed, bound to the administrator and login
session, and expires after 30 minutes. It travels in a hidden form field; it is not
encrypted. No uploaded data is persisted in the session/database during preview.
CSRF protection and admin permission checks apply to both stages. Successful apply
redirects to avoid browser form resubmission.

### Full student sync

```csv
email,registration_number,name,hostel,room_number,gender,contact_number,parent_contact,year,picture
student@example.com,20260001,Example Student,A Hostel,101,M,9876543210,9876543211,1,https://ik.imagekit.io/example/student.jpg
```

Replace the example hostel with an existing exact `Hostel.name`. Email is trimmed
and matched case-insensitively against users and student profiles. New users and
stored Student.email values use lowercase. Existing user accounts, email spelling,
passwords and account flags are preserved. New accounts have student type and an
unusable password. Missing student profiles reuse existing eligible student users.

New students require name and registration_number. Existing students may be
updated with just email and the columns to change. Missing columns preserve fields.
Explicit blank optional fields clear their values, except blank pictures are always
preserved in full mode. Use picture mode with its explicit option to clear pictures.
Gender accepts M/Male and F/Female in any letter case.

Registration number is the existing Student primary key. Changing an existing
registration number is **rejected**, even if the replacement is unused, to preserve
foreign-key references and pass/scanning identity. Handle identity corrections
separately; this feature does not re-key records. Duplicate registrations in the
file and registrations belonging to another student also block the whole upload.

Staff/superuser, admin, security, other non-student accounts, and accounts with
Admin/Security profiles are protected. Duplicate normalized database emails,
conflicting student/user identities and mismatched linked-account emails are
reported for correction; sync does not guess or relink accounts.

### Hostel / room sync

```csv
email,hostel,room_number
student@example.com,A Hostel,101
```

Only hostel and room_number change. Both columns are required; blank values clear
the corresponding assignment. All uploaded emails must resolve to existing students.

- Default: preserve assignments for students outside the upload.
- **Clear existing hostel/room assignments before applying this file**: one UPDATE
  sets all assignments to NULL, followed by bulk updates for uploaded students.
- **Clear hostel/room only for students not present in this upload**: preserve
  uploaded students until their supplied assignments are applied and clear others.

Only one clearing option may be selected. Preview reports both potential scopes and
the selected clearing count. Counts refer to student records with either assignment
field non-NULL. No student or user records are deleted.

### Picture URL sync

```csv
email,picture
student@example.com,https://ik.imagekit.io/example/student.jpg
```

Only picture changes. Blank URLs preserve pictures unless **Allow blank picture
values to clear existing pictures** is checked (default off). No images are
downloaded and no ImageKit calls occur. URLs must fit the existing field's 200
character limit. All uploaded emails must resolve to existing students.

In hostel and picture modes, other recognized full-sync columns are ignored.
Unknown column names are rejected to catch typos. XLSX uses the active sheet;
store identifiers/phone numbers as text to preserve leading zeros. CSV is always
available; XLSX uses `openpyxl` if installed, without adding a new dependency.

## Implementation and safety

The new service is independent of django-import-export. It loads users, students,
hostels and protected-account IDs in bulk. Creates and updates use batches of 500
(Django further reduces SQL batches for backend limits). Update fields are explicitly
restricted by mode; unchanged rows are skipped. Python CASE-expression construction
is also bounded to 500 objects per bulk_update call. No per-row save, model signals,
student deletion, NightPass writes or violation writes are used.

PostgreSQL sync operations serialize using a transaction advisory lock, with existing
users/students locked for revalidation. SQLite serializes writers and rolls back on
write contention. Existing account uniqueness constraints remain in place. No schema
change adds case-insensitive uniqueness: unrelated concurrent account writers do not
participate in this feature's advisory lock. Avoid concurrent legacy imports or
identity administration during an annual sync. This implementation was tested on
SQLite, not a production PostgreSQL server.

Logging uses `apps.users.student_sync` at INFO with a dedicated console handler,
leaving existing loggers enabled. Each apply records administrator ID, mode, filename,
rows, created/updated/reused/skipped/failed totals, timestamp and duration. Rollbacks
record zero committed creations/updates. Preview logs one summary; successful rows
are not logged individually. Final UI summaries count actual changed rows, new
students/users, reused users for new profiles, assignments cleared and duration.

## Verification

Final verification: all 32 new functional tests and the 15,000-row scale test pass
with both settings modules. `manage.py check` passes with both settings modules;
`makemigrations --check --dry-run` reports no changes using an isolated in-memory
database. No production database was used for testing.

```sh
python manage.py check
python manage.py test apps.users.test_student_sync apps.users.test_student_sync_scale --noinput
python manage.py test apps.users.tests apps.validation.tests apps.nightpass.tests core.test_urls --noinput
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py check
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py test apps.users.test_student_sync apps.users.test_student_sync_scale --noinput
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py test apps.users.tests apps.validation.tests apps.nightpass.tests core.test_urls --noinput
```

Initial 15,000-student benchmark, local Python 3.12 / Django 5.2.7 / SQLite:

| Operation | Seconds |
| --- | ---: |
| Preview new full records | 0.47 |
| Create users + students | 3.48 |
| Full update of seven fields | 20.51 |
| Hostel/room update | 3.40 |
| Picture update | 3.95 |

These are local measurements, not EC2 guarantees. The checked-in scale test prints
timings and query counts and can be rerun on deployment-equivalent hardware. No
timeout changes are required by this feature.

Two existing scan-policy tests fail on both this feature and untouched starting
commit `aee7593` in the current environment:

- `test_each_late_scan_adds_one_violation_on_same_pass` (1 violation versus expected 2)
- `test_second_scan_within_five_minutes_is_blocked_across_locations` (first response false)

The remaining 16 existing tests pass. The protected tests and implementation are
unchanged. New tests separately verify preservation of every scan-state field and
all NightPass/ViolationAuditLog values across all sync modes.

**SCANNING FILES CHANGED: NONE.** Protected areas inspected before editing include
`apps/validation/urls.py`, `views.py`, `services/scan_service.py` (`process_scan`,
scanner context and interval checks), `services/lifecycle.py`, user pass_policy and
deadline services, existing Student/NightPass models, and nightpass booking code.
No models, migrations, scanner URLs, QR code, pass transitions or timers changed.
The only existing admin edits add the Student sync route/link; shared settings only
add a dedicated sync logger.

## Review and deployment

No deployment or production restart has been performed. No migrations are needed.
After reviewing and making the commit available through your normal Git workflow,
use your normal application release procedure to install that reviewed code, then:

```sh
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py check
```

Reload/restart the application's Gunicorn service through your existing release
procedure to load Python changes. The production service name is not recorded here,
so no guessed restart command is provided. There are no new static assets, dependency
requirements, Nginx settings or Gunicorn timeout settings. If openpyxl is absent,
use CSV. A migration or collectstatic run is not required for this feature.

## Changed files

- `apps/users/admin.py`: Student admin route, link and permission context only.
- `apps/users/services/student_sync.py`: parsing, validation and atomic bulk sync.
- `apps/users/student_sync_admin.py`: upload and signed preview/confirmation.
- `apps/users/templates/admin/users/student/change_list.html`: sync link.
- `apps/users/templates/admin/users/student/sync.html`: native admin page.
- `apps/users/test_student_sync.py`: 32 functional and regression tests.
- `apps/users/test_student_sync_scale.py`: 15,000-row workload.
- `core/settings.py`: dedicated summary logger only.
- `docs/student-data-sync.md`: usage, samples, verification and release notes.

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
validation for the rest. In FULL, HOSTEL/ROOM and PICTURE modes, blank physical rows count as skipped footer rows; IDENTITY UPDATE rejects them. Limits: 20,000 physical data rows,
10 MB input. Parsed rows stay server-side; confirmation uses a small token regardless
of dataset size.

Choose mode-specific options on the preview page and update the preview before
confirming. If options change when Confirm Apply is clicked, the page shows a new
preview and requires confirmation again. Apply revalidates current database state
inside `transaction.atomic()` before any writes; errors roll back the entire sync.
Counts may change if another administrator changes data between preview and apply.

Preview rows are stored as JSON in a private temporary directory shared by workers.
The form contains only signed metadata: a random 256-bit ID, administrator ID,
session digest, original creation time, data digest and reviewed options. No file
path or rows are exposed in the token, and no rows are stored in the Django session
or database. The token and original preview expire after 30 minutes; updating
options does not extend that lifetime. CSRF protection and admin permission checks
apply to both stages.

Apply atomically claims the temporary file before calling the existing atomic sync
service, which revalidates database state. Concurrent/replayed confirmations fail
safely. A failed apply restores the preview for retry until expiry; success removes
it and redirects. If filesystem cleanup fails after the transaction commits, the
claim remains unusable and the cleanup failure is logged without reporting a
database rollback.

### Duplicate and footer rows

Preview separates total physical data rows, rows considered for sync, valid rows,
unchanged rows, identical duplicate rows skipped, non-student/footer rows skipped,
conflicting duplicate emails and error rows. Rows considered exclude the two harmless
skip categories; valid rows include unchanged rows. Harmless skips are not errors.

Repeated normalized emails with identical effective values for the selected mode
keep the first physical row. Whitespace, email case and accepted gender aliases
are normalized; columns outside the mode do not create conflicts. Different
values remain fatal, with conflicting row numbers reported. Registration checks
run after harmless duplicates are removed. Protected accounts and unknown hostels
still fail validation, including when their rows are repeated identically.

Only rows with blank normalized email, registration_number **and** name are skipped
as non-student/footer rows, even if URL contains `2`. Any populated identity field
keeps a row in normal validation. Blank rows retain their position for reporting.
Apply repeats these checks using the stored raw student fields inside the existing
transaction.

### Temporary storage and cleanup

By default, storage is an application-specific directory inside the service user's
system temporary directory. All Gunicorn workers running as the same user on the
same host use the same directory. It must not be served by the web server. An
optional Django setting `STUDENT_SYNC_TEMP_DIR` can select a persistent shared path
if your release layout or worker isolation requires one. On POSIX, the directory
must belong to the service user with mode 0700; files are created with mode 0600.
On Windows, use the service user's private temp directory or an equivalent private
ACL. Multi-host deployments would need a common filesystem directory.

New uploads opportunistically remove expired previews. The following command can
also run periodically as the service user with the same settings and temp directory:

```sh
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py cleanup_student_sync
```

Cleanup is nonrecursive and only removes regular random-ID preview files older
than 30 minutes. It skips unrelated filenames and symlinks. Abandoned applying
claims receive an additional 24-hour grace period before cleanup, to avoid
interfering with normal in-flight imports. No database migration is needed.

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

In full and picture modes, `picture`, `URL`, `picture_url` and `image_url` are
case-insensitive aliases for the picture field. Preview shows the original source
header mapped to `picture`, rather than listing it as ignored. Supplying more than
one picture alias in a file is rejected as ambiguous. Blank values retain the same
preserve/explicit-clear behavior for every alias.

Each mode processes only its recognized fields. Other columns, including
`Caretaker Name` and `user`, are safely ignored; their original names are shown in
an **Ignored columns** preview warning so typos remain visible. Ignored values are
never assigned to models. Recognized student fields are retained in temporary
preview storage so raw email, registration_number and name can identify student
rows before mode-specific validation. Unrecognized columns are discarded. Blank
headers and duplicate headers (after trimming/case normalization) still fail.
XLSX uses the active sheet;
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

Duplicate/footer hardening: all 55 Student Sync, storage and scale tests pass under
both `core.settings` (147.513s) and `core.settings_campusconnect` (149.026s).
The new annual-upload test exercises 13,002 physical rows in CSV and XLSX, in full
and picture modes: 13,000 valid students, one identical duplicate skipped and one
identity-free `URL="2"` footer skipped. Apply writes exactly 13,000 students;
repeat previews report 13,000 unchanged rows. Conflicting pictures, missing email
with a registration/name, first-row reporting, protected accounts and unknown
hostels are covered. Both system checks pass. The existing 18-test regression
suite under each settings module has 16 passes and the same two previously
documented scan-policy failures below. No protected code was changed.

Hardening verification: all 47 sync, temporary-storage and scale tests pass under
both `core.settings` and `core.settings_campusconnect`. The combined 65-test runs
each have 63 passes and the two pre-existing scan-policy failures listed below.
Both failures were reproduced again on untouched commit `065985b` under both
settings modules. Django system checks pass. The large-preview test uses 15,000
rows whose previous compressed hidden payload exceeds 1.8 MB; the new token is
under 1 KB. Storage tests also verify access from a separate Python process.

Initial feature verification: all 32 functional tests and the 15,000-row scale test passed
with both settings modules. `manage.py check` passes with both settings modules;
`makemigrations --check --dry-run` reports no changes using an isolated in-memory
database. No production database was used for testing.

```sh
python manage.py check
python manage.py test apps.users.test_student_sync apps.users.test_student_sync_storage apps.users.test_student_sync_scale --noinput
python manage.py test apps.users.tests apps.validation.tests apps.nightpass.tests core.test_urls --noinput
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py check
DJANGO_SETTINGS_MODULE=core.settings_campusconnect python manage.py test apps.users.test_student_sync apps.users.test_student_sync_storage apps.users.test_student_sync_scale --noinput
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
- `apps/users/services/student_sync_storage.py`: private, expiring server-side previews.
- `apps/users/management/commands/cleanup_student_sync.py`: safe stale-file cleanup.
- `apps/users/templates/admin/users/student/change_list.html`: sync link.
- `apps/users/templates/admin/users/student/sync.html`: native admin page.
- `apps/users/test_student_sync.py`: functional and regression tests, including extra columns and large previews.
- `apps/users/test_student_sync_storage.py`: expiry, worker access, integrity, claims and cleanup tests.
- `apps/users/test_student_sync_scale.py`: 15,000-row workload.
- `core/settings.py`: dedicated summary logger only.
- `docs/student-data-sync.md`: usage, samples, verification and release notes.


## IDENTITY UPDATE

Superusers can correct existing identities using `user,registration_number,email`.
`user` is the stable CustomUser ID, never a registration number. Both target
fields are required and nonblank. Emails are trimmed and lowercased; registration
numbers are trimmed and remain case-sensitive. Unchanged rows are accepted.
Preview shows both current email fields, current/requested registration numbers,
requested email, row status, and update/unchanged/error counts without writing data.

Duplicate user IDs, duplicate target registration numbers, case-insensitive
email duplicates, existing owners, missing profiles, and protected accounts block
the entire upload. Swapping identities between existing students is rejected.
Apply revalidates under locks and commits all rows in one transaction. Registration
corrections use the same service as individual StudentAdmin corrections, preserving
CustomUser IDs, student data and foreign-key history. Both email fields update
together. No users or students are created or deleted.

PostgreSQL identity uploads lock the user, student, admin and security
tables against concurrent writes because the existing email uniqueness constraint
is case-sensitive. Reads remain available. SQLite serializes writes; a concurrent
write conflict fails safely and requires a fresh preview/retry. No migration is needed.


### Student type (FULL STUDENT SYNC)

Use [student-full-sync-template.csv](student-full-sync-template.csv); `student_type` is the last column, as in Student admin exports. Canonical values are `HOSTELLER` and `DAY_SCHOLAR`. Imports accept case variants, `hostler`, `day scholar`, and `day-scholar`.

Omitting the column preserves an existing student's type and defaults new students to `HOSTELLER`. A supplied blank or invalid value is rejected. Explicit `DAY_SCHOLAR` clears hostel and room assignments, including assignments supplied in the same row. This does not change the user ID, registration number, pass history, violations, pictures, or active booking state. Full sync still cannot change registration numbers. IDENTITY UPDATE, HOSTEL / ROOM, and PICTURE modes ignore this column and retain their existing behavior.

Configure a Day Scholar CampusResource in Django admin with audience **Day Scholar** and default pass type **Day Scholar (2 Scans)**. Set its own capacity and booking window, then enable display and booking when ready. Hosteller resources retain audience **Hosteller** and pass type **HOSTEL** or **OUTSIDE**. Audience and pass type must agree. Day Scholars use the existing Library scanner for IN then OUT; OUT completes their pass. Global scan windows and Library OUT cutoff still apply; hostel timers and limits do not.

Change student types between active bookings where possible: sync deliberately preserves existing passes, which retain their original workflow. No Day Scholar resource is created by migrations.

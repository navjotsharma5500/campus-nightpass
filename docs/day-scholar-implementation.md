# Day Scholar implementation and validation

Implemented locally on `feature/fast-student-sync`, based on `f6e14019d7e3381d504a4126a31cb751fedcd653`. No commit, push, deployment, or migration against an application database was performed. The pre-existing `identity-update.diff` and `identity-update-full.diff` files were left untouched.

## Architecture and behavior

- `Student.student_type` and `CampusResource.audience_type` have explicit `HOSTELLER` defaults. The migrations add fields and extend pass choices; they do not recreate users/students or create resources.
- Student home filters by audience; booking policy independently rejects audience mismatches with `AUDIENCE_MISMATCH`. Resource model validation, admin forms, admin imports, and booking validation reject inconsistent audience/pass combinations.
- Existing NightPass records, capacity tracking, dashboards, Library scanner, scanner authentication, URLs, QR/barcode behavior, cancellation policy, and five-minute cooldown remain in use.
- Day Scholar flow: **BOOKED (step 1) → Library IN (step 2) → Library OUT (step 4, completed)**. Library OUT sets its timestamp, clears `valid` and `student.has_booked`, and does not create hostel timestamps.
- HOSTEL remains **0 → 1 → 2 → 3 → 4**; OUTSIDE remains **1 → 2 → 3 → 4**. Focused tests exercise both complete scanner sequences.
- Day Scholars skip hostel last-out, hostel quotas, hostel transit deadlines, and hostel-return violations. General booking rules, the global scan window, and the existing late Library OUT violation apply. Stale passes expire without recording a hostel return.
- Shared timer resolution avoids null-hostel crashes. Student, scanner, dashboard, and export formatting supports Day Scholars and displays hostel as N/A. Overall counts include both audiences; Day Scholar bookings waiting for Library IN are not counted as hostel transit.
- FULL sync retains its matching and registration protections. It accepts normalized student types, clears stale hostel/room assignments for explicit DAY_SCHOLAR, preserves existing type when the column is absent, and defaults new students to HOSTELLER. Both StudentResource exports put canonical student_type last. Narrow sync modes and identity update retain their scope.

## Migrations

1. `apps/nightpass/migrations/0008_campusresource_audience_type_and_more.py`: audience_type default HOSTELLER; DAY_SCHOLAR default pass choice.
2. `apps/users/migrations/0027_student_student_type_alter_nightpass_pass_type.py`: student_type default HOSTELLER; DAY_SCHOLAR NightPass choice.

A migration regression test creates historical rows at the prior migration state, applies both migrations, and verifies defaults, stable user/student/pass identities, pictures, violations, unchanged pass state, and no added resource.

## Validation results

| Run | Result | Evidence |
| --- | --- | --- |
| `manage.py test apps.users apps.nightpass apps.validation --noinput` (existing suite) | 98 tests: 96 passed, 2 pre-existing failures | `day-scholar-existing-tests.log` |
| New workflow, sync, and migration tests with normal settings | 26/26 passed | `day-scholar-focused-tests.log` |
| Existing policy/scanner tests plus new tests under `core.settings_campusconnect` | 42 tests at that stage: 40 passed, same 2 pre-existing failures | `day-scholar-campus-tests.log` |
| Final new tests under `core.settings_campusconnect` | 26/26 passed | `day-scholar-focused-campus-tests.log` |
| Clean base policy/scanner tests, using a fresh `git archive HEAD` extraction | 19 tests: 17 passed, same 2 failures | `day-scholar-baseline-tests.log` |
| `manage.py check` | No issues | Command output |
| `manage.py check --settings=core.settings_campusconnect` | No issues | Command output |
| `manage.py makemigrations --check --dry-run` | Exit 0; no changes detected | Command output |
| `git diff --check` | Passed | Command output |

The full existing suite includes the 15,000-row full, hostel/room, and picture sync scale test as well as identity, storage, and authorization tests.

The two unchanged failing tests, reproduced on clean base `f6e14019d7e3381d504a4126a31cb751fedcd653`, are:

- `apps.users.tests.UnifiedNightPassPolicyTests.test_each_late_scan_adds_one_violation_on_same_pass`: expected 2 violation flags, received 1.
- `apps.users.tests.UnifiedNightPassPolicyTests.test_second_scan_within_five_minutes_is_blocked_across_locations`: expected the first scan to succeed, received false. Its fixture updates the scanner profile through a queryset while retaining a cached profile on the user; new regression fixtures reload the user.

No unrelated changes were made to those tests or to the existing Hosteller violation implementation.

## Admin setup after a separately authorized release

1. Apply the two migrations using the normal release process. They have only been applied to isolated test databases here.
2. In Student admin, select **Day Scholar** for the relevant students, or run FULL STUDENT SYNC with `student_type=DAY_SCHOLAR`. Use `docs/student-full-sync-template.csv` as a header-only template. FULL sync clears hostel/room automatically; when editing manually, clear obsolete assignments in the form as well.
3. Create a CampusResource with audience **Day Scholar** and default pass type **Day Scholar (2 Scans)**. Choose a name, description, capacity, and booking start/end times. The existing `type` field retains its original meaning.
4. Enable **is_display** and **is_booking** when ready; leave **booking_complete** unchecked for an available resource. Set global scan hours and Library OUT cutoff through the existing settings.
5. Use the existing Library scanner for both scans. No new scanner account or Day Scholar dashboard is needed. Leave existing Hosteller resources on audience Hosteller with HOSTEL or OUTSIDE pass type.

## Assumptions and limits

- Supplied blank/unknown student_type values are rejected; omitting the column is the backward-compatible option.
- Student type changes deliberately preserve active passes and history. Make type changes between bookings where possible: an existing pass keeps its existing workflow. Do not repurpose a resource's pass configuration while its passes are active; the pre-existing NightPass.save behavior derives pass type from that resource.
- HOSTEL/ROOM mode intentionally remains unchanged, so it can still assign a hostel to any student. Booking and display behavior use explicit student_type, not hostel presence.
- The configured local application database path `data/db.sqlite3` is unavailable. The normal migration drift command therefore warns that it cannot inspect that database's migration history, but exits successfully with no model drift. Migration application and historical-data preservation passed on isolated test databases.
- Tests ran with SQLite; no production database or live scanner hardware was accessed. A pre-existing naive NightPass.end_time warning remains unchanged.

## Exact implementation files changed or added

- `.gitignore`
- `apps/nightpass/admin.py`
- `apps/nightpass/migrations/0008_campusresource_audience_type_and_more.py`
- `apps/nightpass/models.py`
- `apps/nightpass/services/booking_policy.py`
- `apps/nightpass/templates/lmao.html`
- `apps/nightpass/templates/nightpass/admin_dashboard.html`
- `apps/nightpass/test_day_scholar.py`
- `apps/nightpass/views.py`
- `apps/users/admin.py`
- `apps/users/migrations/0027_student_student_type_alter_nightpass_pass_type.py`
- `apps/users/models.py`
- `apps/users/resources.py`
- `apps/users/services/pass_policy.py`
- `apps/users/services/student_sync.py`
- `apps/users/services/student_type.py`
- `apps/users/templates/admin/superuser_student_detail.html`
- `apps/users/templates/admin/superuser_student_list.html`
- `apps/users/templates/admin/users/student/sync.html`
- `apps/users/test_day_scholar_migrations.py`
- `apps/users/test_day_scholar_sync.py`
- `apps/validation/services/lifecycle.py`
- `apps/validation/services/scan_service.py`
- `apps/validation/templates/info.html`
- `apps/validation/templates/nightpass/dashboard_detail.html`
- `apps/validation/templates/nightpass/simple_student_list.html`
- `apps/validation/views.py`
- `docs/day-scholar-implementation.md`
- `docs/student-data-sync.md`
- `docs/student-full-sync-template.csv`

# Night Pass Implementation Plan

## 1. Database Schema Plan

### Core tables
- `students`: profile, policy-cycle `violation_count`, eligibility/block fields.
- `passes`: `pass_type`, `journey_state`, `terminal_outcome`, canonical step timestamps, deadlines, policy snapshot reference.
- `scan_events` (append-only): scanner, operator, expected/applied step, accepted/rejected, reason code, captured/received timestamps.
- `violations`: violation type, pass, student, counted flag, reason code, detection timestamp.
- `policies`: date range, enabled days, capacity, gender/year limits, max violations, last-out cutoff, policy cycle/version.
- `scanners`: scanner role (`HOSTEL_SCANNER` / `LIBRARY_SCANNER`), bound hostel (nullable), heartbeat and health metadata.
- `admin_actions`: overrides/cancellations/resets/unblocks with actor, target, reason, timestamp.
- `alerts`: alert type, severity, source, status, created/resolved timestamps.

### Key relationships
- `student 1..* passes`
- `pass 1..* scan_events`
- `pass 0..* violations`
- `policy 1..* passes` (store policy snapshot id on booking)
- `scanner 1..* scan_events`

### Important indexes
- `passes(student_id, terminal_outcome, journey_state)`
- `passes(date, terminal_outcome)`
- `scan_events(pass_id, scanned_at)`
- `scan_events(student_id, expected_step, scanned_at)`
- `violations(student_id, detected_at, counted_flag)`
- `policies(start_date, end_date, is_active)`
- `alerts(status, severity, created_at)`

## 2. Backend Modules

- `pass_lifecycle_manager`
  - deterministic transitions per pass type
  - terminal state protections
  - canonical timestamp updates
- `scan_processing_service`
  - scanner/operator auth + role checks
  - hostel/library scanner enforcement
  - duplicate scan protection and reason-code responses
- `policy_resolver`
  - one effective policy resolution
  - booking validation in fixed order
- `violation_engine`
  - late/missed violation creation
  - counted vs non-counted behavior
  - student violation counter updates and blocking checks
- `deadline_evaluator_job`
  - periodic deadline checks
  - missed-step violations + expiry transitions

## 3. API Endpoints

### Booking
- `POST /api/passes/book`
- `GET /api/passes/me/active`
- `POST /api/passes/{id}/cancel`

### Scan processing
- `POST /api/scans/process`

### Admin actions
- `POST /api/admin/passes/{id}/override-scan`
- `POST /api/admin/passes/{id}/abort`
- `POST /api/admin/students/{id}/unblock`
- `POST /api/admin/students/{id}/violation-adjust`

### Dashboard queries
- `GET /api/admin/dashboard/live`
- `GET /api/admin/dashboard/violations`
- `GET /api/admin/dashboard/bookings`
- `GET /api/admin/dashboard/scanners`

## 4. Background Jobs

- Deadline evaluator (every 1-5 minutes)
  - detect missed `LIBRARY_IN` / `HOSTEL_IN`
  - record violation + reason code
- Expired pass processor
  - finalize to `EXPIRED` when deadlines are irrecoverably missed
- Alert generator
  - scanner heartbeat loss
  - violation spikes
  - quota saturation
  - unusual override volume

## 5. Admin Dashboard Features

- Live operations board
  - active passes by state
  - inside vs outside split
  - hostel-wise load
- Violations tracking
  - by type/day/hostel/student
  - repeat offenders
  - unresolved cases
- Booking analytics
  - capacity utilization
  - gender/year quota utilization
  - rejection reason breakdown
- Scanner health monitoring
  - online/offline status
  - last heartbeat
  - scans/hour
  - failure/rejection trends
- Admin control panels
  - override scan
  - force cancel/abort
  - block/unblock student
  - violation adjust/reset (audited)

## 6. Recommended Development Order

1. Schema and migrations (`passes`, `scan_events`, `violations`, `policies`, `scanners`, `admin_actions`, `alerts`).
2. Pass lifecycle manager (state transitions + invariants).
3. Policy resolver and booking validator pipeline.
4. Booking APIs with transaction-safe finalization.
5. Scan processing service and scan API (role/location/step enforcement + dedupe).
6. Violation engine integration on transitions.
7. Deadline evaluator and expired pass processor jobs.
8. Alert generation jobs.
9. Admin action APIs with immutable audit logging.
10. Dashboard query APIs and admin views.
11. End-to-end tests for flows, policy conflicts, edge cases, and concurrency.

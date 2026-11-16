# Demo seed data — design

**Date:** 2026-11-16 · **Status:** approved in conversation, awaiting written-spec review
**Part of:** deploying SmartHealth for assignment review on AWS, split into three projects
built in order — (1) **demo seed data** (this document), (2) an umbrella Helm chart with
per-component charts and a "slim" values profile proven on local k3d, (3) Terraform + k3s +
Argo CD + CloudFront on AWS (`ap-southeast-2`, Free plan, one server stopped between reviews).

## 1. Goal

A reviewer who opens the deployed demo — perhaps once or twice a week, for a couple of hours —
sees a clinic network that looks lived-in: months of visits, clinical notes, prescriptions,
test results, cancellations and no-shows, analytics with real trends; and, alongside it, live
activity that genuinely went through the system (Temporal workflows, Kafka events, Celery
emails, traces).

## 2. The principle, and how it is kept

`app/seed.py` refuses to seed appointments: *"Hand-writing a `confirmed` row would manufacture
state the workflow never produced."* That stays true for **live** state. History cannot come
from the workflows — the app correctly refuses to book a past slot (PRD R-3) — so history is
**imported**: written as records migrated from MediNova's previous system and marked as such.

| Data | Produced by | Marked |
| --- | --- | --- |
| Past appointments, visits, notes, prescriptions, test orders and results, billing, analytics | Direct import, consistent with every constraint | `appointments.origin = 'legacy_import'`; everything else hangs off an imported appointment |
| Upcoming bookings, cancellations, reschedules, waitlist joins | The real API, so the real workflows, outbox, Kafka, Celery | Their normal origins (`web`, `front_desk`, `waitlist`) |

`origin` already exists (`app/modules/scheduling/models.py`, values `web`, `front_desk`,
`assistant`, `waitlist`); `legacy_import` is a new value, not a schema change.

## 3. Commands

| Command | Run | Does |
| --- | --- | --- |
| `python -m app.cli demo-seed --password P --api-url URL --allow-demo-data [--timezone TZ]` | Once, at first deploy | Build the network; import history from 6 months ago to yesterday; create slots 2 weeks ahead; book live activity through the API |
| `python -m app.cli demo-refresh --password P --api-url URL --allow-demo-data` | By hand, before a review | Import history from the last imported day to yesterday; extend slots 2 weeks ahead; book fresh live activity |

- **Idempotent.** Network entities are keyed on natural keys (clinic name, licence, MRN, email),
  as `seed` does. History is keyed per day: a day with imported appointments is skipped.
  Re-running either command changes nothing it already did.
- **Deterministic.** History for a calendar day comes from a random generator seeded with that
  date, but the fill-rate trend also uses the import date, so a day's history is determined by
  its date and the date it was imported; within one run, re-planning a day gives identical rows
  (exact tests). Imported clinical state **ages**: each run reviews orders and completes
  prescriptions that the passing time has made due (`app/demo/ageing.py`, idempotent).
- **Guarded.** Both refuse without `--allow-demo-data`. `demo-seed` refuses a database that
  holds patients outside the demo's MRN range, so fictional people are never mixed into real
  data. `--password` (or `SMARTHEALTH_DEMO_PASSWORD`) sets every demo login's password (in production it comes from SSM).
- **Separate from `seed`.** `app/seed.py` and its small network are untouched — the
  integration tests depend on them.
- `--timezone` (default `Australia/Sydney`): all three clinics share it, so "today" and clinic
  hours read naturally in the demo.

## 4. Code layout

`app/demo/` — beside `app/seed.py`, outside `app/modules`, so it may read every module's models
like the seeder does, without a `CROSS_MODULE_ALLOWED` edge.

| File | Purpose |
| --- | --- |
| `network.py` | Clinics, departments, providers, patients, logins, slot window — idempotent |
| `content.py` | Specialty templates: complaints, findings, summaries, diagnoses (ICD-10), medications, test choices. Plain data |
| `history.py` | Per-day generation and import, analytics rows, the single audit entry |
| `live.py` | API client that logs in as demo users and books, cancels, reschedules, joins the waitlist |
| `__init__.py` | `demo_seed()` / `demo_refresh()` orchestration used by `app/cli.py` |

**No slot status is ever updated outside `scheduling/service.py`.** `tests/meta/test_slot_writes.py`
forbids *updates* elsewhere and explicitly allows inserting a slot with its status
(`ProviderSlot(..., status=...)`, as `app/seed.py` does). History only inserts slots, so it needs
no new service function and touches nothing under `app/modules`.

## 5. The network (Medium)

- **3 clinics**, one timezone. **All 10 specialties of the knowledge corpus**
  (`services/knowledge/corpus/specialties.md`): Cardiology, Dermatology, Endocrinology,
  Gastroenterology, General Practice, Neurology, Orthopaedics, Paediatrics, Physiotherapy, ENT —
  so every specialty the assistant names can be booked. **~15 providers**, two for the busiest
  (General Practice, Paediatrics, Physiotherapy), departments per clinic to match.
- **~800 patients**: varied ages (about one in seven a child; Paediatrics sees only children),
  fictional names, `@example.com` emails and phone numbers; most without logins (walk-ins).
  (Revised from ~250 while planning: with ~3,000 appointments, 250 patients would each visit
  about ten times in six months; 800 gives three to four.)
- **Logins** (all with `--password`): 1 admin, 2 front desk, **every provider** (linked), **5
  patients** linked to records chosen for rich history — repeat visits, an active prescription,
  a pending test result. Their emails are listed in the README and on the site.
- **Schedules and slots**: each provider holds **two morning sessions a week** on fixed
  weekdays, 09:00–12:00 in the clinic's timezone — six 30-minute slots a session — for history
  and for the 2 weeks of upcoming slots alike. (Revised from "09:00–17:00 every weekday", which
  over six months is ~17,000 appointments rather than the ~3,000 approved.)

## 6. Imported history

For each past weekday from 6 months ago to yesterday, not already imported:

1. **Slots** for every provider session that day, inserted with their final status (`booked` or `free`).
2. **Fill rate** 55–80% per provider-day: higher for General Practice and Paediatrics, mild
   weekly pattern (Mondays busiest), a slow upward trend across the six months.
3. **Outcome** of each booking: ~82% completed, ~10% cancelled (some on the day), ~8% no-show.
4. **Completed** → a `visits` row (`checked_in_at`, `seen_at`, `completed_at`); wait
   (`seen − checked_in`) 5–40 minutes, longer in the afternoons.
5. **Clinical record** for every completed visit: a **signed** note (version 1) from the
   specialty's templates — complaint, findings, patient summary, 1–2 diagnoses. ~40% carry a
   prescription (status `completed` if older than its duration, else `active`); ~25% a test
   order from `app/modules/clinical/catalogue.py`. Orders older than two weeks are `reviewed`
   with a small generated result document (a minimal PDF in the document volume, its text
   pre-extracted, `extraction_status = 'extracted'`, as `clinical.extract_document` sets it); recent ones stay `ordered` — so providers have
   results to review.
6. **Billing**: completed → `captured`, cancelled → `voided`, no-show → `voided`, amount and
   currency from settings. (No-show → `voided` is what the policy implies; the live app does not
   do it yet — see §10.)
7. **No patient overlap**: a patient is never given two appointments at overlapping times
   (the rule `claim_slot` enforces live).

**Not written by import:** outbox events (no emails about past visits), Temporal workflows,
per-row audit entries. **Written instead:** one Mongo audit entry per import run (date range,
counts, actor `system:demo-import`), and **`analytics_daily`** for each imported day computed
from the same rows — so `analytics.reconcile` reports **zero drift**.

## 7. Live activity

After the history, through Part A's HTTP API (`--api-url`), as demo users:

- Front desk books ~10 appointments for walk-in patients across the next two weeks.
- Each linked patient books one or two of their own.
- One cancellation; one reschedule; two patients join the waitlist for a fully booked
  provider-day, so a cancellation there promotes them.

Live bookings nobody checks in become no-shows through `VisitWorkflow` once their slot passes
(on a stopped server, when it restarts), so recent no-show rates run higher than the imported
history's. A reviewer can check patients in and complete visits to avoid that. Each such
no-show keeps its billing hold (PRD R-33). The live phase tops up to 12 upcoming appointments
to keep the effect small.

Each booking waits for its workflow to confirm (polling `GET /appointments/{id}`), so the
command fails visibly if the system is not healthy. Everything is visible afterwards in Temporal
(`booking-…`, `visit-…`, `cancel-…`), Jaeger, Mailpit and the Async backbone dashboard.

## 8. Testing

| Tier | Proves |
| --- | --- |
| unit | Same date → same history; rates and fill within bounds; templates cover all 10 specialties; no patient overlap in a generated day |
| integration (test stack) | Importing 10 days violates no constraint; running twice adds nothing; `analytics.reconcile` reports zero drift for those days; `demo-seed` refuses a database with non-demo patients and refuses without `--allow-demo-data`; the live phase confirms bookings through real workflows |
| meta | `test_slot_writes` still passes (slot writes only in `scheduling/service.py`) |

A test case YAML under `tests/cases/` (the core-path Stop hook: `app/cli.py` and
`app/modules/**` change). README, the documentation site and ARCHITECTURE describe the two
commands and the `legacy_import` marker (the docs Stop hook).

## 9. Out of scope

Assistant conversations and AI-usage history (they appear once reviewers use the assistant);
Grafana/Jaeger/Prometheus history (telemetry is not seeded); real email delivery (Mailpit keeps
every message on the server); changing `seed`.

## 10. Finding raised while designing (not fixed here)

**A live no-show never releases its payment hold.** `service.mark_no_show` (called by the
`visit.no_show` activity) changes the appointment and emits `appointments.no_show`, but nothing
calls `billing.release` — so the billing record stays `authorized` indefinitely, and the
patient-facing policy (`services/knowledge/corpus/policies.md`, "Payment and the card hold")
says nothing about no-shows. Imported history uses `voided` for no-shows, the behaviour the
policy implies. Whether a no-show should be voided or charged, and fixing the live path, is a
separate decision and a separate change, recorded as PRD risk R-33 (open).

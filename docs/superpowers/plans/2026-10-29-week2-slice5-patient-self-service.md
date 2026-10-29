# Week 2 Slice 5 — Patient Self-Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close PRD R-5: a patient lists their own appointments (the read half, `GET /patients/me`, landed with the frontend). Staff get the same list with filters. A CLI command links a patient login to its record, replacing the SQL `UPDATE` the README currently documents. The demo client gains an Appointments page with Cancel.

**Architecture:** `GET /appointments` in `scheduling`: the patient role is forced to its own patient id (resolved through `patients.public.patient_id_for_user`; no linked record → 404, spec §4.6); other roles may filter by `patient_id`, `provider_id`, `status`, and a slot window `from`/`to` (`AwareDatetime`, on `provider_slots.starts_at` — a scheduling-owned table, so no boundary crossing). Paginated `Page[AppointmentRead]`, newest slot first. The spec's `?mine=true` is unnecessary — a patient's list is always their own — and the spec is amended.

**Spec:** `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §4.6. **Branch:** `feat/patient-self-service` (from `main` at 16230b0).

## Facts
- Boundary: `scheduling/service.py` imports no other module; the router may use `app.modules.patients.public` (declared pair). `app/cli.py` is a core path for the Stop hook — the cases in Task 4 cover the working set.
- `AppointmentRead` exists (`scheduling/schemas.py`); `Page`/`page_params` in `app.core.pagination`/`app.api.deps`.
- Appointments with no slot yet (`pending` before claim, `failed` early) have no `starts_at`: order `starts_at DESC NULLS LAST, created_at DESC, id`; the window filter excludes slot-less rows only when `from`/`to` is given.
- Roles: `GET /appointments` — patient (own), provider, front_desk, admin. Provider per-object scoping remains the documented gap R-14.

### Task 0: Spec
Amend §4.6: `GET /appointments` (no `mine` flag; patient forced to own; staff filters; 404 for a patient login with no record). Commit `docs: define the appointment list for patients and staff`.

### Task 1: `GET /appointments`
- `service.list_appointments(session, *, patient_id, provider_id, status, start, end, page) -> (rows, total)` — join `ProviderSlot` (outer) for the window and ordering.
- Router: `@router.get("", response_model=Page[AppointmentRead])` — mind route order (it must not shadow `/{appointment_id}`; `""` vs `"/{id}"` is fine). Patient: `own = patient_id_for_user(...)`; `None` → `NotFound("no patient record is linked to this login")`; ignore any `patient_id` query param a patient sends (or 403 if it differs — choose 403 and test it).
- T1: role gates; patient with a different `patient_id` → 403; naive datetime → 422; bad status → 422.
- T3: a patient sees exactly their own (not another patient's); ordering newest slot first with slot-less rows last; staff filters by patient/provider/status/window; totals match filters; patient with no record → 404.
- Mutation proofs: drop the patient scoping → a T3 test fails; drop `NULLS LAST` → ordering test fails.
- Commit `feat: list appointments, a patient's own or staff-filtered`.

### Task 2: `link-patient` CLI
`python -m app.cli link-patient --email <login> --mrn <MRN>`: sets `patients.user_id` for the patient with that MRN to the user with that email. Errors (exit 1, clear message): unknown email, unknown MRN, user role not `patient`, patient already linked to a different user, user already linked to another patient (unique `patients.user_id`). Idempotent for the same pair. Audit (`patient` entity, `account_linked`, actor `system:cli`). Follow `app/cli.py`'s existing structure (argparse subcommands, session handling, exit codes) and fix its module docstring ("the only way to put a login into the system" — `seed` creates logins too, and now `link-patient` links them). Implement the DB part as a function in `patients/service.py` (`link_user(session, audit, *, mrn, user_id, actor_user_id)`) — the CLI may read the user by email via `identity` code the way `create-user` does (check how `app/cli.py` already imports; the CLI is not a domain module, so the boundary rule does not apply). T0 for argument parsing; T3 for every error path and the happy path (then `GET /patients/me` as that login works). Commit `feat: link a patient login to its record from the CLI`.

### Task 3: Demo client
- `frontend/src/pages/Appointments.tsx`: patient → "My appointments" (no filters); staff/provider → filters (status select, patient search reuse optional — keep it to status + provider picker if simple) — `GET /appointments?...`, paginated with the existing `Pager`, empty state. Each row: slot time (if any — the list returns `slot_id`; showing time needs the slot — **add `starts_at` to `AppointmentRead` only if cheap**, i.e. populated via the join for the list and via a lookup for the single read; otherwise show created time and a link to `/bookings/:id`), status badge, link to the booking status page, and **Cancel** (patient own / front_desk / admin) when status is `pending` or `confirmed` → `POST /appointments/{id}/cancel` with a reason prompt (an inline small form, not `window.prompt`), then refetch; errors verbatim.
- Nav: "Appointments" for every role. Badge styles in `styles.css` for `cancelled`, `rescheduled`, `completed`, `no_show`.
- Vitest: a small test for any new pure helper (e.g. `canCancel(status, role)`); `npm test`, `npm run build`.
- Commit `feat: an appointments page in the demo client, with cancel`.

### Task 4: Verify, demo, cases
All lanes, ruff, route_check, `npm test`/`build`. Rebuild the dev stack (`--build`, `FRONTEND_PORT=5174` if needed); link a patient login with the new CLI; in the browser-equivalent via API: patient lists own appointments, cancels one; staff lists with filters. Cases: `pat-004-a-patient-lists-only-their-own-appointments` (integration, P0, `PART-A-FR-1`), `ops-004-the-cli-links-a-patient-login` (integration, P1, `PART-A-FR-1`) — verify ids. `route_check --write-catalog`. Commit `test: add cases for the appointment list and patient linking`.

### Task 5: Docs, review, land
PRD: R-5 → **Closed** (both halves, tests named); FR-1/FR-2 rows note the list; §10 Week 2 → **Complete**; §11 recount. README: endpoint row, a "link a patient login" step replacing the SQL, the Appointments page. ARCHITECTURE: §1.4 entries, §2 data flow for the list if the style warrants. CLAUDE.md: status "Week 2 complete", commands table gains `link-patient`, counts. Then an independent review, fixes, and land only with the user's go-ahead.

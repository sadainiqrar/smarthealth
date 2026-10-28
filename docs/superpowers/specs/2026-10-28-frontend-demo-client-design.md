# Frontend Demo Client — Design

**Date:** 2026-10-28
**Status:** Approved (design); implementation plan `docs/superpowers/plans/2026-10-28-frontend-demo-client.md`
**Depends on:** Week 2 slices 1–2 (`2026-10-28-week2-scheduling-workflows-design.md`) — booking exists.

---

## 1. Purpose and scope

A small browser client so the system can be *run*, not just tested: log in, find patients
and providers, book an appointment and watch it go `pending → confirmed | failed`. It is a
**demo client**, not a graded deliverable — the assignment's stack is backend-only — so it
stays small and makes no architectural claims of its own. The API remains the authority on
every rule; the UI only hides what the API would refuse.

**In scope**

- Backend: `GET /slots` (free, future slots, filterable, paginated) and `GET /patients/me`.
- `frontend/`: Vite + React 18 + TypeScript. Screens: Login, Patients, Providers, Book,
  Booking status.
- Compose: a `frontend` service under `--profile app` — nginx serving the build and
  proxying `/api` to `app`.
- Docs: README run instructions; PRD records the client as a demo, not a requirement.

**Out of scope**

- Visits, cancel/reschedule, waitlist screens — they arrive with slices 3–4.
- An appointments list ("my bookings") — slice 5 (`GET /appointments?mine`).
- Provider/patient editing in the UI; patient registration is the only write besides booking.
- Browser end-to-end tests; generated API types; a UI component library.

## 2. Decisions

| Id | Decision | Rejected alternative | Why |
| --- | --- | --- | --- |
| **F-1** | The browser reaches the API through a same-origin `/api` proxy (Vite in dev, nginx in compose) | CORS middleware on FastAPI | No backend change, no allowed-origins list to keep right, and the token never crosses origins |
| **F-2** | The token lives in `sessionStorage`; the role and subject are read by decoding the JWT payload for **display only** | An `/auth/me` endpoint; `localStorage` | Decoding needs no new endpoint and is safe because the API re-verifies every request. `sessionStorage` clears with the tab, which suits a demo on a shared machine |
| **F-3** | Hand-written TypeScript types for the five response shapes | OpenAPI code generation | Five small types do not justify a generator, its config and its build step |
| **F-4** | `GET /slots` lives in `scheduling` and returns only slot fields — no provider names | Joining provider names into the response | A join would make `scheduling/service.py` import `providers`, which rule 4 of the boundary test forbids. The UI picks a provider first (from `GET /providers`, readable by every role) and then lists that provider's slots |
| **F-5** | `GET /patients/me` is built now, pulled forward from slice 5 | Leave patient-role booking out of the UI | The approved design lets a patient book for themselves, and the UI cannot know the patient's own id without it. It closes the read half of PRD R-5 |

## 3. Backend additions

### 3.1 `GET /slots`

- Query: `provider_id` (required — the UI always has one, and an unfiltered list of every
  clinic's slots is not a useful page), `from` and `to` (optional ISO datetimes; default
  `from = now`), `limit`/`offset` via the existing `page_params`.
- Returns `Page[SlotRead]`, `SlotRead = {id, provider_id, clinic_id, department_id,
  starts_at, ends_at}`, ordered by `starts_at`.
- Only `status = 'free'` **and** `starts_at > now` — the same two conditions the claim
  enforces, so a listed slot is one the claim could take (it may still lose a race).
- Roles: every role (`tuple(UserRole)`), like `GET /providers`.
- Lives in `scheduling/service.py` (`list_free_slots`) and `scheduling/router.py`
  (a second `APIRouter(prefix="/slots")`). Read-only, so the slot-write guard is unaffected.

### 3.2 `GET /patients/me`

- Role `patient` only. Resolves `users.id → patients.user_id` through a new
  `patients/service.get_patient_for_user`; 404 when the login has no linked patient record.
  (`patients/public.patient_id_for_user` answers the same question for other modules, but
  returns only the id, and the router serves the patients module's own record.)
- Declared **before** `GET /patients/{patient_id}` in the router, or FastAPI would try to
  parse `me` as a UUID and answer 422.

## 4. Frontend

```
frontend/
  index.html, package.json, tsconfig.json, vite.config.ts   Vite + React + TS, proxy /api → :8000
  src/main.tsx            QueryClient, Router
  src/api.ts              fetch wrapper: base /api, bearer token, {error,detail} → ApiError
  src/auth.tsx            session context: token in sessionStorage, decoded role/subject, logout
  src/types.ts            Page<T>, Patient, Provider, Slot, Appointment, BookingAccepted
  src/poll.ts             isTerminal(status) — the polling stop condition
  src/pages/Login.tsx, Patients.tsx, Providers.tsx, Book.tsx, Booking.tsx
  src/styles.css          one small hand-written stylesheet
  src/*.test.ts           Vitest: api error handling, token decoding, poll stop condition
  Dockerfile, nginx.conf  multi-stage: node build → nginx serve + /api proxy
```

**Screens**

| Screen | Who | What |
| --- | --- | --- |
| Login | anyone | email + password → token; errors show the API's `detail` |
| Patients | front_desk, admin, provider | search + paginate; front_desk/admin see a register form |
| Providers | every role | search + paginate, read-only |
| Book | patient, front_desk, admin | staff: search and pick a patient; a patient: themselves via `/patients/me`. Pick a provider, then a free slot, optional reason → `POST /appointments` with a generated `Idempotency-Key` (`crypto.randomUUID()`), then navigate to the status page |
| Booking status | anyone who can read it | polls `GET /appointments/{id}` every second while `status === "pending"`; stops on any other status; shows `confirmed_at` or the failure |

**Behaviour rules**

- A 401 from any call clears the session and returns to Login.
- Every error renders `{error}: {detail}` from the API envelope verbatim — the UI never
  invents its own wording for a server rule.
- Navigation shows only what the role may do; the API still enforces it.
- Poll on `status`, never on `in_progress` (README documents why).

## 5. Running it

- **Dev:** `cd frontend && npm install && npm run dev` → `http://localhost:5173`, with the
  backend on `:8000` (either `uvicorn` locally or the compose `app` service).
- **Compose:** `docker compose --profile app -f docker-compose.infra.yml up -d --build --wait`
  also starts `frontend` on `${FRONTEND_PORT:-5173}`; nginx proxies `/api/` → `http://app:8000/`.

## 6. Testing

| Where | Proves |
| --- | --- |
| T1 contract | `/slots` and `/patients/me` role gates and validation (422 on a bad `provider_id`; `/patients/me` not shadowed by `/{patient_id}`) |
| T3 integration | `/slots` lists only free future slots of that provider, in order; a booked slot disappears from it; `/patients/me` returns the linked record and 404s without one |
| Vitest | `api.ts` turns the error envelope into `ApiError`, a 401 triggers the unauthorised hook; JWT payload decoding; `isTerminal` |
| Manual | the compose demo: log in, book, watch it confirm — recorded in the plan's last task |

Cases: one each for `GET /slots` and `GET /patients/me`, so the Stop hook has a validated
case for the backend paths this touches.

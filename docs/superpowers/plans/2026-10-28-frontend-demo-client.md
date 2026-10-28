# Frontend Demo Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small React client that runs the system end to end — log in, browse patients and providers, book an appointment and watch it confirm — plus the two read endpoints it needs.

**Architecture:** Backend gains `GET /slots` (scheduling) and `GET /patients/me` (patients), both read-only. `frontend/` is Vite + React 18 + TypeScript with React Router and TanStack Query, talking to the API through a same-origin `/api` proxy (Vite in dev, nginx in compose). The API is the authority on every rule.

**Tech Stack:** FastAPI/SQLAlchemy (existing); Node 22, Vite 5, React 18, TypeScript 5, react-router-dom 6, @tanstack/react-query 5, Vitest 2 + jsdom; nginx 1.27.

**Spec:** `docs/superpowers/specs/2026-10-28-frontend-demo-client-design.md`.

**Branch:** `feat/frontend` (exists; the spec and this plan are its first commit).

---

## Facts the implementer needs

1. **Boundary rules** (`tests/meta/test_import_boundaries.py`): `service.py`/`models.py`/`schemas.py` import no other domain module. Routers are framework-bound and must be listed in `FRAMEWORK_BOUND`. `GET /slots` lives entirely in `scheduling`; it must not join provider names.
2. **The slot-write guard** (`tests/meta/test_slot_writes.py`) flags any `.status` assignment in an `app/` file that imports `ProviderSlot` or `SlotStatus`, outside `scheduling/service.py`. Put the slot query in `service.py`; the router imports neither name.
3. **Route order:** `GET /patients/me` must be declared before `GET /patients/{patient_id}`, or FastAPI parses `me` as a UUID and answers 422.
4. **`from` is a Python keyword:** declare it as `from_: datetime | None = Query(default=None, alias="from")`.
5. **Stop hook:** changes under `app/modules/**` need a validated case in `tests/cases/` in the same working set (Task 4).
6. **Roles and claims:** the JWT payload has `sub` (user id) and `role` (`patient` | `provider` | `front_desk` | `admin`) and `exp`. `GET /providers` is readable by every role; `GET /patients` by `front_desk`, `admin`, `provider`; `POST /patients` by `front_desk`, `admin`.
7. **Error envelope:** every failure is `{"error": <name>, "detail": <string>}`, except FastAPI request-validation 422s, whose `detail` may be a list — render it with `JSON.stringify` if not a string.
8. **Line endings:** new files LF (`git ls-files --eol`). Windows + Git Bash.

## File map

| File | Action |
| --- | --- |
| `app/modules/scheduling/service.py` | add `list_free_slots` |
| `app/modules/scheduling/schemas.py` | add `SlotRead` |
| `app/modules/scheduling/router.py` | add `slots_router` |
| `app/api/router.py` | mount `slots_router` |
| `app/modules/patients/service.py` | add `get_patient_for_user` |
| `app/modules/patients/router.py` | add `GET /patients/me` |
| `tests/tiers/t1_contract/test_slots_contract.py`, `test_patients_me_contract.py` | create |
| `tests/tiers/t3_integration/test_slots_api.py`, `test_patients_me_api.py` | create |
| `tests/cases/slt-001-*.yaml`, `pat-003-*.yaml` | create |
| `frontend/**` | create |
| `docker-compose.infra.yml`, `.gitignore` | modify |
| `README.md`, `docs/PRD.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md` | modify |

---

### Task 0: Commit the spec and plan

- [ ] `git add docs/superpowers/specs/2026-10-28-frontend-demo-client-design.md docs/superpowers/plans/2026-10-28-frontend-demo-client.md` and commit `docs: design and plan a demo frontend client` (reasoned body; every commit in this plan ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`).

---

### Task 1: `GET /slots`

**Files:** `app/modules/scheduling/{service,schemas,router}.py`, `app/api/router.py`, `tests/tiers/t1_contract/test_slots_contract.py`, `tests/tiers/t3_integration/test_slots_api.py`

- [ ] **Step 1: Failing contract tests** — `tests/tiers/t1_contract/test_slots_contract.py`:

```python
import uuid
from datetime import UTC, datetime

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=str(uuid.uuid4()), role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


async def test_listing_slots_requires_a_token(api_client):
    response = await api_client.get(f"/slots?provider_id={uuid.uuid4()}")
    assert response.status_code == 401


async def test_provider_id_is_required(api_client):
    response = await api_client.get("/slots", headers=auth(UserRole.PATIENT))
    assert response.status_code == 422


async def test_a_malformed_provider_id_is_rejected(api_client):
    response = await api_client.get(
        "/slots?provider_id=not-a-uuid", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 422
```

Run → FAIL (404, no route).

- [ ] **Step 2: Failing integration tests** — `tests/tiers/t3_integration/test_slots_api.py`. Read `tests/tiers/t3_integration/scheduling_helpers.py` first and use its `seed_bookable` / `add_slot` (and its cleanup convention):

```python
"""GET /slots against real Postgres: only free, future slots of the provider, in order."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.scheduling.models import ProviderSlot, SlotStatus
from tests.tiers.t3_integration.scheduling_helpers import add_slot, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_only_free_future_slots_of_the_provider_are_listed(api, token_for, factory):
    async with factory() as session:
        b = await seed_bookable(session)                       # free, tomorrow
        later = await add_slot(session, b, starts_in=timedelta(days=2))
        past = await add_slot(session, b, starts_in=timedelta(hours=-2))
        taken = await add_slot(session, b, starts_in=timedelta(days=3))
        await session.execute(
            update(ProviderSlot).where(ProviderSlot.id == taken).values(status=SlotStatus.BOOKED)
        )
        await session.commit()
        other = await seed_bookable(session)                    # another provider

    response = await api.get(
        f"/slots?provider_id={b.provider_id}", headers=token_for(UserRole.PATIENT)
    )
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(b.slot_id), str(later)]
    assert str(past) not in ids and str(taken) not in ids and str(other.slot_id) not in ids
    assert response.json()["total"] == 2


async def test_the_window_narrows_the_list(api, token_for, factory):
    async with factory() as session:
        b = await seed_bookable(session)
        await add_slot(session, b, starts_in=timedelta(days=5))
    tomorrow_end = (await _starts(factory, b.slot_id)) + timedelta(hours=1)
    response = await api.get(
        "/slots",
        params={"provider_id": str(b.provider_id), "to": tomorrow_end.isoformat()},
        headers=token_for(UserRole.FRONT_DESK),
    )
    assert [item["id"] for item in response.json()["items"]] == [str(b.slot_id)]


async def _starts(factory, slot_id):
    async with factory() as session:
        return (await session.get(ProviderSlot, slot_id)).starts_at
```

Adapt to the helper's real return types (e.g. whether `add_slot` returns an id or a `ProviderSlot`) — the assertions are what matter. Setting `status` directly in a **test** is fine: the slot-write guard scans `app/` only.

Run (stack up) → FAIL.

- [ ] **Step 3: Implement.** In `app/modules/scheduling/schemas.py`:

```python
class SlotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_id: uuid.UUID
    clinic_id: uuid.UUID
    department_id: uuid.UUID | None
    starts_at: datetime
    ends_at: datetime
```

In `app/modules/scheduling/service.py` (add `func` to the sqlalchemy import, `PageParams` from `app.core.pagination`):

```python
async def list_free_slots(
    session: AsyncSession, *, provider_id: uuid.UUID, now: datetime,
    start: datetime | None, end: datetime | None, page: PageParams,
) -> tuple[list[ProviderSlot], int]:
    """Free slots of one provider that have not started, earliest first.

    The same two conditions `claim_slot` enforces -- `status = 'free'` and
    `starts_at > now` -- so every listed slot is one a booking could take. It can still
    lose a race to another booking; the claim, not this list, is the authority.
    """
    conditions = [
        ProviderSlot.provider_id == provider_id,
        ProviderSlot.status == SlotStatus.FREE,
        ProviderSlot.starts_at > max(now, start) if start else ProviderSlot.starts_at > now,
    ]
    if end is not None:
        conditions.append(ProviderSlot.starts_at < end)
    total = await session.scalar(
        select(func.count()).select_from(ProviderSlot).where(*conditions)
    )
    rows = await session.scalars(
        select(ProviderSlot)
        .where(*conditions)
        .order_by(ProviderSlot.starts_at, ProviderSlot.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)
```

(Write the `starts_at` condition plainly — compute `lower = max(now, start) if start else now` first.)

In `app/modules/scheduling/router.py`, after the appointments routes:

```python
slots_router = APIRouter(prefix="/slots", tags=["slots"])


@slots_router.get("", response_model=Page[SlotRead])
async def list_slots(
    provider_id: uuid.UUID,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    _: TokenClaims = Depends(require_role(*UserRole)),
) -> Page[SlotRead]:
    slots, total = await service.list_free_slots(
        session, provider_id=provider_id, now=clock.now(), start=from_, end=to, page=page
    )
    return Page[SlotRead](
        items=[SlotRead.model_validate(s) for s in slots],
        total=total, limit=page.limit, offset=page.offset,
    )
```

Imports: `datetime`, `Query`, `page_params` from `app.api.deps`, `Page`/`PageParams` from `app.core.pagination`, `Clock`/`get_clock` from `app.core.clock`, `SlotRead`. Mount in `app/api/router.py`: `api_router.include_router(scheduling_router.slots_router)`.

- [ ] **Step 4: Run** contract + integration + `tests/meta` → pass. **Commit** `feat: list free future slots for a provider`.

---

### Task 2: `GET /patients/me`

**Files:** `app/modules/patients/{service,router}.py`, `tests/tiers/t1_contract/test_patients_me_contract.py`, `tests/tiers/t3_integration/test_patients_me_api.py`

- [ ] **Step 1: Failing tests.**

Contract (`test_patients_me_contract.py`, reuse the `auth` helper pattern from Task 1):

```python
async def test_me_requires_a_token(api_client):
    assert (await api_client.get("/patients/me")).status_code == 401


@pytest.mark.parametrize("role", [UserRole.FRONT_DESK, UserRole.PROVIDER, UserRole.ADMIN])
async def test_me_is_for_patients_only(api_client, role):
    """403, not 422: proves /me is routed before /{patient_id}."""
    assert (await api_client.get("/patients/me", headers=auth(role))).status_code == 403
```

Integration (`test_patients_me_api.py`): create a `User` (role PATIENT) and a `Patient` linked by `user_id`, commit; `GET /patients/me` with `token_for(UserRole.PATIENT, subject=str(user.id))` → 200 and `id == patient.id`; a PATIENT token whose subject has no linked record → 404. Clean up the rows you create (follow the cleanup pattern in `test_booking_round_trip.py`'s `patient_login` fixture).

- [ ] **Step 2: Implement.** `app/modules/patients/service.py`:

```python
async def get_patient_for_user(session: AsyncSession, user_id: uuid.UUID) -> Patient:
    """The patient record linked to a login. 404 for a login with none -- a patient
    account created before its record was linked, or a misissued role."""
    patient = await session.scalar(select(Patient).where(Patient.user_id == user_id))
    if patient is None:
        raise NotFound("no patient record is linked to this login")
    return patient
```

`app/modules/patients/router.py` — **above** `get_patient`:

```python
@router.get("/me", response_model=PatientRead)
async def get_my_patient_record(
    session: AsyncSession = Depends(get_session),
    claims: TokenClaims = Depends(require_role(UserRole.PATIENT)),
) -> PatientRead:
    """Declared before /{patient_id}: otherwise FastAPI parses "me" as a UUID and 422s."""
    patient = await service.get_patient_for_user(session, uuid.UUID(claims.subject))
    return PatientRead.model_validate(patient)
```

- [ ] **Step 3: Run** → pass. **Commit** `feat: let a patient read their own record (PRD R-5, read half)`.

---

### Task 3: Frontend scaffold, API client, auth

**Files:** `frontend/{package.json,tsconfig.json,vite.config.ts,index.html}`, `frontend/src/{main.tsx,api.ts,auth.tsx,types.ts,poll.ts,styles.css}`, tests `frontend/src/{api,auth,poll}.test.ts`, `.gitignore`

- [ ] **Step 1: `frontend/package.json`**

```json
{
  "name": "smarthealth-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run"
  },
  "dependencies": {
    "@tanstack/react-query": "^5.59.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.27.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.11",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.2",
    "jsdom": "^25.0.1",
    "typescript": "^5.6.3",
    "vite": "^5.4.9",
    "vitest": "^2.1.3"
  }
}
```

Then `cd frontend && npm install` (commit `package-lock.json`). Add `frontend/node_modules/` to the root `.gitignore` (`dist/` is already ignored).

- [ ] **Step 2: Config files.**

`frontend/vite.config.ts`:

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Same-origin /api proxy (spec F-1): no CORS on the backend, and the token never
// crosses origins. nginx does the same in the compose image.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.SMARTHEALTH_API ?? "http://localhost:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: { environment: "jsdom" },
});
```

`frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "noEmit": true
  },
  "include": ["src", "vite.config.ts"]
}
```

`frontend/index.html`: standard Vite HTML with `<div id="root"></div>` and `<script type="module" src="/src/main.tsx"></script>`, title `SmartHealth`.

- [ ] **Step 3: Failing tests.**

`frontend/src/api.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorisedHandler } from "./api";

function respond(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("api", () => {
  it("prefixes /api and sends the bearer token", async () => {
    sessionStorage.setItem("smarthealth.token", "t0k");
    respond(200, { ok: true });
    await api("/providers");
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/providers");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t0k");
  });

  it("turns the error envelope into an ApiError", async () => {
    respond(409, { error: "Conflict", detail: "slot is no longer free" });
    await expect(api("/x")).rejects.toMatchObject({
      status: 409, error: "Conflict", detail: "slot is no longer free",
    });
  });

  it("stringifies a validation detail list", async () => {
    respond(422, { detail: [{ loc: ["body", "slot_id"], msg: "missing" }] });
    const err = (await api("/x").catch((e) => e)) as ApiError;
    expect(err.status).toBe(422);
    expect(err.detail).toContain("slot_id");
  });

  it("calls the unauthorised handler on 401", async () => {
    const handler = vi.fn();
    setUnauthorisedHandler(handler);
    respond(401, { error: "InvalidCredentials", detail: "missing or invalid credentials" });
    await expect(api("/x")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledOnce();
  });
});
```

`frontend/src/auth.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { decodeToken } from "./auth";

function token(payload: object): string {
  const b64 = (o: object) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64(payload)}.sig`;
}

describe("decodeToken", () => {
  it("reads role, subject and expiry", () => {
    expect(decodeToken(token({ sub: "u1", role: "front_desk", exp: 2000000000 }))).toEqual({
      subject: "u1", role: "front_desk", expiresAt: 2000000000 * 1000,
    });
  });

  it("returns null for garbage", () => {
    expect(decodeToken("not.a.jwt")).toBeNull();
  });
});
```

`frontend/src/poll.test.ts`:

```ts
import { expect, it } from "vitest";
import { isTerminal } from "./poll";

it("keeps polling only while pending", () => {
  expect(isTerminal("pending")).toBe(false);
  for (const s of ["confirmed", "failed", "cancelled", "completed", "no_show", "rescheduled"]) {
    expect(isTerminal(s)).toBe(true);
  }
});
```

Run `npm test` → FAIL (modules missing).

- [ ] **Step 4: Implement.**

`frontend/src/api.ts`:

```ts
// One fetch wrapper for the whole client. Every failure becomes an ApiError carrying the
// API's own {error, detail} -- the UI shows server rules verbatim, never its own wording.

export const TOKEN_KEY = "smarthealth.token";

export class ApiError extends Error {
  constructor(public status: number, public error: string, public detail: string) {
    super(`${error}: ${detail}`);
  }
}

let onUnauthorised: () => void = () => {};
export function setUnauthorisedHandler(handler: () => void): void {
  onUnauthorised = handler;
}

export function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

type Options = Omit<RequestInit, "body"> & { json?: unknown };

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const { json, ...init } = options;
  const headers = new Headers(init.headers);
  const token = readToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body: BodyInit | undefined;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const response = await fetch(`/api${path}`, { ...init, headers, body });
  if (!response.ok) {
    let error = "HTTPError";
    let detail = response.statusText;
    try {
      const payload = await response.json();
      error = payload.error ?? error;
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      // non-JSON body (e.g. a proxy error page): keep the status text
    }
    if (response.status === 401) onUnauthorised();
    throw new ApiError(response.status, error, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}
```

`frontend/src/auth.tsx`:

```tsx
// Session state. The token is kept in sessionStorage (cleared with the tab). Its payload
// is decoded for DISPLAY only -- which nav items to show. The API re-verifies every
// request, so a forged payload buys nothing (spec F-2).
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { TOKEN_KEY, api, readToken, setUnauthorisedHandler } from "./api";

export type Role = "patient" | "provider" | "front_desk" | "admin";
export interface Session {
  subject: string;
  role: Role;
  expiresAt: number;
}

export function decodeToken(token: string): Session | null {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(part.padEnd(part.length + ((4 - (part.length % 4)) % 4), "=")));
    if (typeof payload.sub !== "string" || typeof payload.role !== "string") return null;
    return { subject: payload.sub, role: payload.role as Role, expiresAt: payload.exp * 1000 };
  } catch {
    return null;
  }
}

interface AuthValue {
  session: Session | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

function store(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage unavailable (private mode): the session lasts until reload
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => {
    const token = readToken();
    const decoded = token ? decodeToken(token) : null;
    return decoded && decoded.expiresAt > Date.now() ? decoded : null;
  });

  const logout = useCallback(() => {
    store(null);
    setSession(null);
  }, []);

  useEffect(() => setUnauthorisedHandler(logout), [logout]);

  const login = useCallback(async (email: string, password: string) => {
    const { access_token } = await api<{ access_token: string }>("/auth/login", {
      method: "POST",
      json: { email, password },
    });
    store(access_token);
    setSession(decodeToken(access_token));
  }, []);

  const value = useMemo(() => ({ session, login, logout }), [session, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}
```

`frontend/src/poll.ts`:

```ts
// Poll a booking while it is pending. Stop on any other status. Never key on
// `in_progress`: it is true only before the appointment row exists (README).
export const POLL_MS = 1000;
export const isTerminal = (status: string): boolean => status !== "pending";
```

`frontend/src/types.ts` — hand-written mirrors of the Pydantic read models (spec F-3):

```ts
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface Patient {
  id: string; mrn: string; first_name: string; last_name: string;
  date_of_birth: string | null; phone: string | null; email: string | null;
}
export interface Provider {
  id: string; first_name: string; last_name: string; specialty: string; is_active: boolean;
}
export interface Slot {
  id: string; provider_id: string; clinic_id: string; department_id: string | null;
  starts_at: string; ends_at: string;
}
export interface Appointment {
  id: string; status: string; in_progress: boolean; patient_id: string | null;
  provider_id: string | null; slot_id: string | null; reason: string | null;
  confirmed_at: string | null; created_at: string | null;
}
export interface BookingAccepted { appointment_id: string; status: string }
```

Run `npm test` → pass.

- [ ] **Step 5: Commit** `feat: scaffold the demo client with its API wrapper and session`.

---

### Task 4: Screens

**Files:** `frontend/src/main.tsx`, `frontend/src/styles.css`, `frontend/src/pages/{Login,Patients,Providers,Book,Booking}.tsx`; `tests/cases/slt-001-*.yaml`, `tests/cases/pat-003-*.yaml`

- [ ] **Step 1: `main.tsx`.** `QueryClientProvider` (`retry: false` for queries — let errors show), `AuthProvider`, `BrowserRouter`. A `Layout` component with a header: app name, nav links filtered by role (table below), the role and a Logout button. Routes: `/login`, `/patients`, `/providers`, `/book`, `/bookings/:id`. Any route except `/login` redirects to `/login` when there is no session. `/` redirects to the first nav item for the role.

| Nav item | Roles |
| --- | --- |
| Patients | front_desk, admin, provider |
| Providers | every role |
| Book | patient, front_desk, admin |

- [ ] **Step 2: Pages** — each uses `useQuery`/`useMutation` with `api<T>()`; every error renders `<p className="error">{err.message}</p>` (the `ApiError` message is `"{error}: {detail}"`).
  - **Login:** email, password, submit → `login()` → navigate to `/`.
  - **Patients:** search input (debounced 300 ms) → `GET /patients?search=&limit=20&offset=` with Prev/Next; table of MRN, name, DOB, phone. front_desk/admin see a register form (`mrn`, `first_name`, `last_name`, optional `date_of_birth`, `phone`, `email`) → `POST /patients`, then invalidate the list.
  - **Providers:** search (debounced) → `GET /providers?search=&limit=20&offset=`; table of name, specialty, active.
  - **Book:** patient picker — for staff a search box over `GET /patients` with selectable results; for a patient, `GET /patients/me` (show "your record"; on 404 show the API's message). Provider picker over `GET /providers?search=`. Once a provider is chosen, `GET /slots?provider_id=...&limit=50` listed with local date/time (`toLocaleString`). Optional reason textarea. Submit → `POST /appointments` with header `Idempotency-Key: crypto.randomUUID()` generated **once per form submission attempt and reused if the user clicks Retry** (keep it in state, reset when patient/slot changes) → navigate to `/bookings/{appointment_id}`.
  - **Booking:** `useQuery(["appointment", id], ..., { refetchInterval: (q) => q.state.data && isTerminal(q.state.data.status) ? false : POLL_MS })`. Show status as a badge (pending / confirmed / failed …), `confirmed_at`, slot id, and on `failed` a line "Booking failed — choose another slot" with a link back to Book.
- [ ] **Step 3: `styles.css`** — one small stylesheet: system font, max-width container, header nav, tables, form rows, `.error` in red, status badges. Light and dark via `prefers-color-scheme`. No framework.
- [ ] **Step 4:** `npm run build` (type-checks) and `npm test` → pass.
- [ ] **Step 5: Cases** for the backend endpoints, authored with the `smarthealth-testcase` skill or by hand in the style of `tests/cases/bkg-*.yaml`; verify requirement ids exist in `docs/PRD.md`:
  - `slt-001-only-free-future-slots-are-listed` — integration, P1, `PART-A-FR-2`, impl `tests/tiers/t3_integration/test_slots_api.py::test_only_free_future_slots_of_the_provider_are_listed`.
  - `pat-003-a-patient-reads-their-own-record` — integration, P1, `PART-A-FR-1`, impl the 200 test in `test_patients_me_api.py`.
  `python -m tests.runner.route_check --write-catalog` → none unsupported/blocked.
- [ ] **Step 6: Commit** `feat: add login, patients, providers and booking screens` (frontend) and `test: add cases for slot listing and the patient's own record` (cases).

---

### Task 5: Container

**Files:** `frontend/Dockerfile`, `frontend/nginx.conf`, `frontend/.dockerignore`, `docker-compose.infra.yml`

- [ ] **Step 1:**

`frontend/nginx.conf`:

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;

    # Same-origin API (spec F-1): /api/x -> http://app:8000/x
    location /api/ {
        proxy_pass http://app:8000/;
        proxy_set_header Host $host;
    }

    # Client-side routes fall back to the SPA.
    location / {
        try_files $uri /index.html;
    }
}
```

`frontend/Dockerfile`:

```dockerfile
# Build with Node, serve with nginx: the runtime image carries no Node and no sources.
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
```

`frontend/.dockerignore`: `node_modules`, `dist`.

- [ ] **Step 2: Compose service** (after `worker`):

```yaml
  frontend:
    # The demo client: nginx serving the built SPA and proxying /api to `app`.
    profiles: ["app"]
    build: ./frontend
    ports: ["${FRONTEND_PORT:-5173}:80"]
    depends_on:
      app:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost/"]
      interval: 10s
      timeout: 5s
      retries: 6
```

- [ ] **Step 3: Verify** — `docker compose --profile app -f docker-compose.infra.yml up -d --build --wait`; all services healthy; `curl -s localhost:5173/ | head` returns the HTML; `curl -s localhost:5173/api/health` returns the health JSON. Commit `feat: serve the demo client from compose behind an nginx /api proxy`.

---

### Task 6: Manual demo, then docs

- [ ] **Step 1: Run the whole thing in a browser** with the compose stack (use the Chrome tools if available, else describe exact clicks): seed (`app.cli seed`), log in as a seeded front-desk user (or create one with `app.cli create-user`), browse patients and providers, register a patient, book a slot, watch the status page move to `confirmed`. Then create a `patient` login linked to a patient (link via SQL `UPDATE patients SET user_id=... WHERE id=...` in the `postgres` container), log in as them, confirm Book shows "your record", book, confirm. Record what happened, including anything awkward. Bring the stack down without `-v`.
- [ ] **Step 2: Docs** (verify every claim against code):
  - `README.md`: a "Frontend" section — dev (`cd frontend && npm install && npm run dev`, backend on :8000) and compose (`http://localhost:5173`); endpoint table gains `GET /slots` (every role) and `GET /patients/me` (patient).
  - `docs/PRD.md`: record the client as a demo, not a requirement (a short non-goal/scope note); R-5 → "read half closed by `GET /patients/me`; appointment list remains slice 5".
  - `docs/ARCHITECTURE.md`: §1.4 mentions `frontend/` as a client outside the modular monolith, talking only to the HTTP API through `/api`; add the two endpoints to the scheduling and patients module entries.
  - `CLAUDE.md`: commands table gains `cd frontend && npm run dev` and `npm test`; note `frontend/` is a demo client.
  Commit `docs: document the demo client and its two endpoints`.

### Task 7: Verify and land

`python -m pytest -m "not docker" -q`, `python -m pytest -m docker -q`, `python -m ruff check app tests`, `python -m tests.runner.route_check`, `cd frontend && npm test && npm run build`. Land on `main` (fast-forward + push) **only with the user's go-ahead**.

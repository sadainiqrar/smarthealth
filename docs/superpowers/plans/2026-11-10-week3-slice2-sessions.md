# Week 3 slice 2 — Sessions, signing keys, throttling: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the 60-minute stateless HS256 token with short EdDSA-signed access tokens
bound to a server-side session in Redis, with refresh-token rotation, logout, revocation of
every session of a deactivated user, a JWKS endpoint for the Part B services, login
throttling that keeps the no-enumeration guarantee, and per-user rate limits on booking.

**Spec:** `docs/superpowers/specs/2026-11-10-weeks3-5-architecture-design.md` §4.1–4.3, D-24, D-25.

## Decisions taken while planning (recorded in the spec when the slice lands)

1. **One session = one refresh family.** `sid` is both the session id carried in the access
   token and the refresh-token family. A session has an absolute lifetime
   (`session_hours`, 12) that refreshing does not extend.
2. **Refresh tokens are `<sid>.<secret>`**, opaque. Redis keeps only the SHA-256 of the
   current secret and of the previous one. Presenting the *previous* secret is reuse — a
   stolen token was replayed — and revokes the session. Presenting anything else is a plain
   401. Rotation is one Lua script, so two concurrent refreshes cannot both win.
3. **The refresh token travels in the response body**, not an `HttpOnly` cookie. The demo
   client keeps it in `sessionStorage` beside the access token. A cookie needs CSRF
   handling for a SPA served from another origin in dev; it is the production choice and is
   recorded as deferred, not silently dropped.
4. **Stores are behind two small interfaces**, `SessionStore` and `RateLimiter`, each with a
   Redis implementation and an in-memory one. The contract tier has no containers, so it
   overrides the FastAPI dependencies with the in-memory stores
   (`tests/harness/auth.py`); the integration tier uses real Redis.
5. **The second Redis instance (`redis-cache`) arrives with its first user**, the slot
   cache in slice 5. This slice configures the existing `redis` as the *state* instance:
   `noeviction` and AOF persistence (D-24).
6. **Deactivating a login** is a CLI command (`python -m app.cli deactivate-user --email …`)
   — no endpoint manages users today. `identity.sessions_revoked` is published in slice 3,
   when the outbox exists.
7. **Signing keys.** `Settings.jwt_private_key` holds an Ed25519 PEM. A development key is
   the default, as the HS256 secret was, and the API refuses to start with it when
   `environment` is `production`. `python -m app.cli generate-signing-key` prints a new one.
   The JWKS `kid` is the RFC 7638 thumbprint of the public key.

## Tasks

- [ ] **1. Settings and dependency.** `pyjwt[crypto]`. Settings: `jwt_private_key` (dev PEM),
  `jwt_issuer`, `access_token_minutes=10`, `session_hours=12`, `login_max_failures=5`,
  `login_ip_max_failures=50`, `login_window_minutes=15`, `booking_rate_limit_per_minute=10`.
  Remove `jwt_secret`, `jwt_algorithm`, `jwt_expiry_minutes`. T0 tests.
- [ ] **2. `security.py` on EdDSA.** `create_access_token(..., session_id=…)` adds `sid`, `iss`,
  `kid` header; `decode_access_token` verifies signature, `exp`, `iss`, requires `sid`;
  `public_jwk(settings)`, `generate_signing_key()`. T0 tests rewritten for keys.
- [ ] **3. `sessions.py`.** `Session`, `SessionStore` protocol; `RedisSessionStore`
  (`session:<sid>` hash, `user-sessions:<user_id>` set, Lua rotation) and
  `InMemorySessionStore`; `issue`, `is_active`, `rotate` → `Rotated | Reused | Unknown`,
  `revoke`, `revoke_user`. Shared behaviour tests run against both (T0 in-memory, T3 Redis).
- [ ] **4. `throttle.py`.** `RateLimiter` protocol: fixed-window `hit(key, limit, window) ->
  Decision(allowed, retry_after)`, `peek`, `reset`; Redis (`INCR` + `EXPIRE` in a
  pipeline) and in-memory. `RateLimited` domain error (429, `Retry-After`).
- [ ] **5. Service.** `authenticate` takes the throttle and the store: a throttled address or IP
  still pays the dummy argon2 verify and answers the identical 401; failures count, success
  resets the address counter; success issues a session and returns access + refresh.
  `refresh`, `logout`, `logout_all`, `deactivate_user`.
- [ ] **6. Routes and deps.** `POST /auth/login` (adds `refresh_token`), `POST /auth/refresh`,
  `POST /auth/logout`, `POST /auth/logout-all`, `GET /.well-known/jwks.json`.
  `require_role` checks the session after the signature. `get_session_store`,
  `get_rate_limiter` dependencies over `app.state`. Booking and reschedule routes take a
  per-user rate limit.
- [ ] **7. Tests move to sessions.** `tests/harness/auth.py` (`issue_token`, in-memory stores,
  overrides on the shared `app`); the contract call sites and the T3 `token_for` fixture use
  it. New T1 contract tests (refresh, logout, revoked session, rate limit 429, JWKS shape,
  throttled login indistinguishable); T3 against Redis (rotation, reuse, logout-all,
  deactivate, login lockout).
- [ ] **8. CLI.** `deactivate-user`, `generate-signing-key`. Production guard in the lifespan.
- [ ] **9. Compose.** `redis` with `--appendonly yes --maxmemory-policy noeviction`; drop
  `SMARTHEALTH_JWT_SECRET`.
- [ ] **10. Frontend.** Keep the refresh token; on a 401 refresh once and retry; logout calls
  `/auth/logout`. Vitest.
- [ ] **11. Cases, docs, verification.** Cases `aut-003` … ; PRD (Week 1 §11 deferral closed,
  R-7 partly), ARCHITECTURE §2.2, README, CLAUDE.md, spec notes; ruff, fast lane, docker
  lane, `npm test`.

"""Contract for POST /auth/login.

Every test here runs without a database. The login route's only interesting
behaviour that *needs* one — a real credential check returning a token or an
`InvalidCredentials` 401 — is proven by the integration test in the same task as
case `aut-001`, not here. From a plain test process Postgres is genuinely
unreachable (the compose stack sits on the offset port 15432 while `Settings`
defaults to 5432), and `httpx.ASGITransport` re-raises application exceptions
rather than turning them into a 500 — so a T1 test that reaches the service layer
does not observe a status code at all, it errors out with `ConnectionRefusedError`.
"""

import pytest

pytestmark = pytest.mark.contract


async def test_login_requires_email_and_password(api_client):
    response = await api_client.post("/auth/login", json={})
    assert response.status_code == 422


async def test_login_rejects_a_malformed_email(api_client):
    response = await api_client.post(
        "/auth/login", json={"email": "not-an-email", "password": "x"}
    )
    assert response.status_code == 422


async def test_login_is_not_behind_an_auth_gate(api_client):
    """422 rather than 401: request validation ran, so no gate preceded it.

    `require_role` reads the Authorization header while FastAPI is still solving
    dependencies, which happens before the body is validated — a guarded route
    answers an invalid body with 401 and never reports the body's problems. Sending
    a garbage bearer token as well as no token at all covers both halves of that
    gate: a missing token and an undecodable one take different branches, and
    neither may produce a 401 here.
    """
    without_token = await api_client.post("/auth/login", json={})
    assert without_token.status_code == 422

    with_garbage_token = await api_client.post(
        "/auth/login",
        json={},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert with_garbage_token.status_code == 422


async def test_login_is_published_in_the_openapi_schema(api_client):
    """The route is mounted and documented, not merely reachable by accident.

    This deliberately does not assert the absence of a `security` requirement:
    `require_role` is a plain dependency that reads the header off the `Request`
    rather than a FastAPI security scheme, so it contributes nothing to the schema
    and *every* route in this application — guarded or not — omits `security`.
    Such an assertion would pass whether or not the route were public, which is
    why the gate is proven behaviourally above instead.
    """
    response = await api_client.get("/openapi.json")
    assert response.status_code == 200
    assert "post" in response.json()["paths"]["/auth/login"]

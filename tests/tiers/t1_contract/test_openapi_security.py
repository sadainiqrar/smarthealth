"""`/docs` can authorise: every role-gated route declares the bearer scheme in OpenAPI.

`require_role` reads the token itself, so the schema learnt nothing from it and Swagger
UI had no Authorize button -- while the README told readers to "authorise once, then
execute any endpoint". It now also depends on `HTTPBearer(auto_error=False)`, which
contributes the scheme and nothing else (the 401/403 behaviour is pinned by
test_authz.py and the error-envelope tests, unchanged).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


@pytest.fixture
async def schema(api_client) -> dict:
    response = await api_client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _bearer_schemes(schema: dict) -> list[str]:
    schemes = schema.get("components", {}).get("securitySchemes", {})
    return [
        name for name, scheme in schemes.items()
        if scheme.get("type") == "http" and scheme.get("scheme", "").lower() == "bearer"
    ]


async def test_a_bearer_scheme_is_published(schema):
    assert _bearer_schemes(schema), "no HTTP bearer scheme in components.securitySchemes"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/appointments", "post"),
        ("/appointments", "get"),
        ("/patients", "post"),
        ("/providers", "get"),
        ("/slots", "get"),
    ],
)
async def test_a_gated_route_requires_the_bearer_scheme(schema, path, method):
    [bearer] = _bearer_schemes(schema)
    security = schema["paths"][path][method].get("security", [])
    assert {bearer: []} in security, f"{method.upper()} {path} does not declare {bearer}"


@pytest.mark.parametrize(
    ("path", "method"), [("/auth/login", "post"), ("/health", "get"), ("/ready", "get")]
)
async def test_a_public_route_requires_nothing(schema, path, method):
    assert "security" not in schema["paths"][path][method]


async def test_every_operation_that_declares_security_uses_only_the_bearer_scheme(schema):
    """No stray scheme: one way in, and it is the one `require_role` actually checks."""
    [bearer] = _bearer_schemes(schema)
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for requirement in operation.get("security", []):
                assert set(requirement) == {bearer}, f"{method.upper()} {path}: {requirement}"

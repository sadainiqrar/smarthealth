"""The fake payment gateway behaves like a real one where Part A depends on it.

Contract tier: the mock is an ASGI app, driven in-process. What matters is the
idempotency contract (a retried request never charges twice) and the chaos switches the
reliability tests use.
"""

import asyncio

import httpx
import pytest

from services.payments_mock.app import app

pytestmark = pytest.mark.contract


@pytest.fixture
async def gateway():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        await client.post("/_reset")
        yield client
        await client.post("/_reset")


def _auth(customer: str = "p1") -> dict:
    return {"amount_minor": 5000, "currency": "USD", "customer_ref": customer, "reference": "a1"}


async def test_a_repeated_authorization_with_one_key_is_one_authorization(gateway):
    first = await gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "k1"})
    again = await gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "k1"})
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    other = await gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "k2"})
    assert other.json()["id"] != first.json()["id"]


async def test_capture_void_and_refund_follow_the_authorization_state(gateway):
    auth = (await gateway.post("/authorizations", json=_auth(),
                               headers={"Idempotency-Key": "a"})).json()
    captured = await gateway.post(f"/authorizations/{auth['id']}/capture",
                                  headers={"Idempotency-Key": "c"})
    assert captured.json()["status"] == "captured"
    void = await gateway.post(f"/authorizations/{auth['id']}/void",
                              headers={"Idempotency-Key": "v"})
    assert void.status_code == 409, "a captured charge is refunded, not voided"
    refund = await gateway.post("/refunds", json={"authorization_id": auth["id"]},
                                headers={"Idempotency-Key": "r"})
    assert refund.json()["status"] == "refunded"


async def test_a_named_customer_is_declined(gateway):
    await gateway.post("/_chaos", json={"decline_customers": ["unlucky"]})
    response = await gateway.post("/authorizations", json=_auth("unlucky"),
                                  headers={"Idempotency-Key": "d"})
    assert response.json()["status"] == "declined"


async def test_a_failure_before_the_effect_changes_nothing(gateway):
    await gateway.post("/_chaos", json={"failure_rate": 1.0})
    failed = await gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "f"})
    assert failed.status_code == 503
    await gateway.post("/_chaos", json={})
    retried = await gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "f"})
    assert retried.status_code == 201 and retried.json()["status"] == "authorized"


async def test_a_timeout_after_the_charge_is_answered_by_the_retry(gateway):
    """The hard case: it took effect, but the caller gave up waiting. The retry with the
    same key must get the original answer, not a second charge."""
    await gateway.post("/_chaos", json={"timeout_after_commit_rate": 1.0, "hang_seconds": 5})
    # In-process the transport has no socket to time out; the caller giving up is the
    # same thing as a client timeout from the gateway's side.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "t"}), 0.2
        )
    await gateway.post("/_chaos", json={})
    retried = await gateway.post("/authorizations", json=_auth(), headers={"Idempotency-Key": "t"})
    assert retried.json()["status"] == "authorized"


async def test_a_request_without_a_key_is_refused(gateway):
    assert (await gateway.post("/authorizations", json=_auth())).status_code == 400

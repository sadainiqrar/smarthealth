"""A fake payment gateway, standing in for an external provider (Weeks 3-5 spec D-27).

It belongs to no service and shares no store: Part A reaches it over HTTP only, as it
would a real gateway. Its value is in behaving like one where it matters:

- **Idempotency keys are honoured.** A request repeated with the same `Idempotency-Key`
  gets the first answer back and changes nothing. That is what makes Temporal's activity
  retries safe: a retry can never charge twice.
- **It misbehaves on request** (`POST /_chaos`): added latency; a rate of 503s *before*
  anything happens; a rate of timeouts *after* the charge is taken -- the hardest case, where
  the caller cannot tell whether it worked and must retry with the same key; and declines
  for named customers, so a booking's compensation path can be shown on demand.

State is in memory: a restart forgets everything, which a demo can live with.

    uvicorn services.payments_mock.app:app --port 8090
"""

from __future__ import annotations

import asyncio
import random
import threading
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

app = FastAPI(title="payments-mock", version="1.0")


class Chaos(BaseModel):
    latency_ms: int = Field(default=0, ge=0)
    #: Fraction of requests answered 503 before any effect.
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Fraction of requests that take effect, then answer only after this long -- past any
    #: sane client timeout. The retry, with the same key, gets the stored answer.
    timeout_after_commit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    hang_seconds: float = Field(default=10.0, ge=0.0)
    #: Authorisations for these customer references are declined.
    decline_customers: list[str] = Field(default_factory=list)


class AuthorizationRequest(BaseModel):
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    customer_ref: str
    reference: str


class RefundRequest(BaseModel):
    authorization_id: str


_lock = threading.Lock()
_chaos = Chaos()
_transactions: dict[str, dict[str, Any]] = {}
_responses: dict[str, tuple[int, dict[str, Any]]] = {}
_random = random.Random()


async def _misbehave_before() -> None:
    if _chaos.latency_ms:
        await asyncio.sleep(_chaos.latency_ms / 1000)
    if _random.random() < _chaos.failure_rate:
        raise HTTPException(status_code=503, detail="gateway unavailable (chaos)")


async def _misbehave_after() -> None:
    if _random.random() < _chaos.timeout_after_commit_rate:
        await asyncio.sleep(_chaos.hang_seconds)


def _idempotent(key: str | None, operation: str):
    """The stored answer for `key`, scoped by operation, or None. A key reused for a
    different operation is a client bug and is refused."""
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    stored = _responses.get(key)
    if stored is not None and stored[1].get("operation") != operation:
        raise HTTPException(status_code=422, detail="Idempotency-Key reused for another operation")
    return stored


def _remember(key: str, status: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    _responses[key] = (status, body)
    return status, body


def _answer(response: Response, stored: tuple[int, dict[str, Any]]) -> dict[str, Any]:
    response.status_code = stored[0]
    return stored[1]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/authorizations", status_code=201)
async def authorize(
    body: AuthorizationRequest, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    with _lock:
        stored = _idempotent(idempotency_key, "authorize")
    if stored:
        return _answer(response, stored)
    await _misbehave_before()
    with _lock:
        stored = _idempotent(idempotency_key, "authorize")
        if stored is None:
            declined = body.customer_ref in _chaos.decline_customers
            transaction = {
                "id": f"auth_{uuid.uuid4().hex[:16]}",
                "operation": "authorize",
                "status": "declined" if declined else "authorized",
                "amount_minor": body.amount_minor,
                "currency": body.currency,
                "customer_ref": body.customer_ref,
                "reference": body.reference,
            }
            if declined:
                transaction["decline_reason"] = "card declined"
            _transactions[transaction["id"]] = transaction
            stored = _remember(idempotency_key, 201, dict(transaction))
    await _misbehave_after()
    return _answer(response, stored)


def _transition(authorization_id: str, operation: str, allowed: set[str], status: str):
    transaction = _transactions.get(authorization_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="no such authorization")
    if transaction["status"] == status:
        return transaction
    if transaction["status"] not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"cannot {operation} an authorization that is {transaction['status']}",
        )
    transaction["status"] = status
    return transaction


async def _operate(
    authorization_id: str, operation: str, allowed: set[str], status: str,
    key: str | None, response: Response,
) -> dict[str, Any]:
    with _lock:
        stored = _idempotent(key, operation)
    if stored:
        return _answer(response, stored)
    await _misbehave_before()
    with _lock:
        stored = _idempotent(key, operation)
        if stored is None:
            transaction = _transition(authorization_id, operation, allowed, status)
            stored = _remember(key, 200, {**transaction, "operation": operation})
    await _misbehave_after()
    return _answer(response, stored)


@app.post("/authorizations/{authorization_id}/capture")
async def capture(
    authorization_id: str, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _operate(authorization_id, "capture", {"authorized"}, "captured",
                          idempotency_key, response)


@app.post("/authorizations/{authorization_id}/void")
async def void(
    authorization_id: str, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _operate(authorization_id, "void", {"authorized"}, "voided",
                          idempotency_key, response)


@app.post("/refunds")
async def refund(
    body: RefundRequest, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _operate(body.authorization_id, "refund", {"captured"}, "refunded",
                          idempotency_key, response)


@app.get("/transactions/{transaction_id}")
async def get_transaction(transaction_id: str) -> dict[str, Any]:
    with _lock:
        transaction = _transactions.get(transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="no such transaction")
    return transaction


@app.get("/_chaos")
async def get_chaos() -> Chaos:
    return _chaos


@app.post("/_chaos")
async def set_chaos(chaos: Chaos) -> Chaos:
    global _chaos
    _chaos = chaos
    return _chaos


@app.post("/_reset")
async def reset(request: Request) -> dict[str, str]:
    """Forget every transaction and stored answer, and switch chaos off. For tests."""
    global _chaos
    with _lock:
        _transactions.clear()
        _responses.clear()
        _chaos = Chaos()
    return {"status": "reset"}

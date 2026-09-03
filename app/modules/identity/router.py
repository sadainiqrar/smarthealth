"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, get_clock
from app.db.session import get_session
from app.modules.identity.schemas import LoginRequest, TokenResponse
from app.modules.identity.service import authenticate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> TokenResponse:
    token, expires_in = await authenticate(
        session,
        email=body.email,
        password=body.password,
        settings=request.app.state.settings,
        now=clock.now(),
    )
    return TokenResponse(access_token=token, expires_in=expires_in)

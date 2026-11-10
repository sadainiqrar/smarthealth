"""Request and response bodies for authentication."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    #: Seconds until `access_token` expires; refresh before then.
    expires_in: int
    #: Single use: `POST /auth/refresh` spends it and returns a new one. Presenting a
    #: spent one ends the session.
    refresh_token: str


class LogoutResponse(BaseModel):
    sessions_ended: int


class Jwk(BaseModel):
    kty: str
    crv: str
    x: str
    kid: str
    alg: str
    use: str


class JwksResponse(BaseModel):
    keys: list[Jwk]


class UserDirectoryEntry(BaseModel):
    """A login as an admin sees it in a directory: who, not credentials."""

    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    #: From the linked patient or provider record; None for staff with neither.
    name: str | None

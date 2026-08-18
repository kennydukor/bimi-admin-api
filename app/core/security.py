"""
Authentication & authorisation.

Sessions ride on an httpOnly cookie holding an opaque, random token; the token
maps to a row in `admin_sessions`. Passwords are bcrypt-hashed. Role gating is
expressed as FastAPI dependencies so a route simply declares
`user = Depends(require_super_admin)` and the 403 is handled for it.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg
import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, status

from app.core.config import settings
from app.db.session import db


# ── Password hashing ─────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(
        plain.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    ).decode()


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── Session tokens ───────────────────────────────────────────
def new_session_token() -> str:
    return secrets.token_urlsafe(32)


# ── Current user ─────────────────────────────────────────────
@dataclass
class CurrentUser:
    id: str
    email: str
    full_name: str
    role: str
    status: str

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"


async def get_current_user(
    conn: asyncpg.Connection = Depends(db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    # Two ways to authenticate, so both same-site (cookie) and cross-origin
    # (SPA on another origin, e.g. localhost → deployed API) setups work:
    #   1. the httpOnly session cookie, or
    #   2. an "Authorization: Bearer <token>" header carrying the same token.
    # The header path avoids third-party-cookie / SameSite restrictions that
    # block cookies when the frontend and API are on different sites.
    token = session_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    session_token = token

    row = await conn.fetchrow(
        """
        SELECT u.id, u.email, u.full_name, u.role, u.status, s.expires_at
        FROM admin_sessions s
        JOIN admin_users u ON u.id = s.user_id
        WHERE s.token = $1
        """,
        session_token,
    )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")
    if row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    if row["status"] == "suspended":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended.")

    return CurrentUser(
        id=str(row["id"]),
        email=row["email"],
        full_name=row["full_name"],
        role=row["role"],
        status=row["status"],
    )


async def require_super_admin(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not user.is_super_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super Admin access required.")
    return user

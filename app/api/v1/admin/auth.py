"""Auth endpoints: login / logout / me / password reset / email verify."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.config import settings
from app.core.security import (
    CurrentUser,
    get_current_user,
    hash_password,
    new_session_token,
    verify_password,
)
from app.db.session import db
from app.schemas.admin import (
    EmailRequest,
    LoginRequest,
    ResetConfirmRequest,
    Session,
    TokenRequest,
    User,
)
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(row: asyncpg.Record) -> User:
    return User(
        id=str(row["id"]),
        email=row["email"],
        full_name=row["full_name"],
        role=row["role"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        last_login_at=row["last_login_at"].isoformat() if row["last_login_at"] else None,
    )


@router.post("/login", response_model=Session)
async def login(
    body: LoginRequest, response: Response, conn: asyncpg.Connection = Depends(db)
):
    row = await conn.fetchrow(
        "SELECT * FROM admin_users WHERE email = $1", body.email.lower().strip()
    )
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")
    if row["status"] == "suspended":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended.")
    if row["status"] == "pending_verification":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Please verify your email first.")

    token = new_session_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    await conn.execute(
        "INSERT INTO admin_sessions (token, user_id, expires_at) VALUES ($1,$2,$3)",
        token, row["id"], expires,
    )
    await conn.execute(
        "UPDATE admin_users SET last_login_at = now() WHERE id = $1", row["id"]
    )
    user = _user_out(row)
    await audit.record(
        conn,
        actor=CurrentUser(user.id, user.email, user.full_name, user.role, user.status),
        action="login", target="",
    )

    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "development",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return Session(user=user, expires_at=expires.isoformat())


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(db),
):
    await conn.execute("DELETE FROM admin_sessions WHERE user_id = $1", user.id)
    response.delete_cookie(settings.session_cookie_name, path="/")


@router.get("/me", response_model=User)
async def me(
    user: CurrentUser = Depends(get_current_user), conn: asyncpg.Connection = Depends(db)
):
    row = await conn.fetchrow("SELECT * FROM admin_users WHERE id = $1", user.id)
    return _user_out(row)


@router.post("/password-reset", status_code=status.HTTP_204_NO_CONTENT)
async def request_password_reset(
    body: EmailRequest, conn: asyncpg.Connection = Depends(db)
):
    row = await conn.fetchrow("SELECT id FROM admin_users WHERE email = $1", body.email)
    # Always 204 — don't leak which emails exist.
    if row is not None:
        token = secrets.token_urlsafe(32)
        await conn.execute(
            """INSERT INTO admin_auth_tokens (token, user_id, purpose, expires_at)
               VALUES ($1,$2,'password_reset',$3)""",
            token, row["id"], datetime.now(timezone.utc) + timedelta(hours=1),
        )
        # TODO: hand `token` to the email service.


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    body: ResetConfirmRequest, conn: asyncpg.Connection = Depends(db)
):
    row = await conn.fetchrow(
        """SELECT * FROM admin_auth_tokens
           WHERE token=$1 AND purpose='password_reset' AND used_at IS NULL""",
        body.token,
    )
    if row is None or row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reset link is invalid or expired.")
    await conn.execute(
        "UPDATE admin_users SET password_hash=$1, status='active' WHERE id=$2",
        hash_password(body.password), row["user_id"],
    )
    await conn.execute(
        "UPDATE admin_auth_tokens SET used_at=now() WHERE token=$1", body.token
    )


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(body: TokenRequest, conn: asyncpg.Connection = Depends(db)):
    row = await conn.fetchrow(
        """SELECT * FROM admin_auth_tokens
           WHERE token=$1 AND purpose='verify_email' AND used_at IS NULL""",
        body.token,
    )
    if row is None or row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Verification link is invalid or expired.")
    await conn.execute(
        "UPDATE admin_users SET status='active' WHERE id=$1", row["user_id"]
    )
    await conn.execute(
        "UPDATE admin_auth_tokens SET used_at=now() WHERE token=$1", body.token
    )

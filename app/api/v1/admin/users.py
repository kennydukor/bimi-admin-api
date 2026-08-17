"""Users — Super Admin only. Invite, role, suspend/reinstate, remove."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import CurrentUser, require_super_admin, hash_password
from app.db.session import db
from app.schemas.admin import (
    InviteUserRequest,
    Paginated,
    SetRoleRequest,
    User,
)
from app.services import audit

router = APIRouter(prefix="/users", tags=["users"])

PAGE_SIZE = 20


def _generate_temp_password(length: int = 12) -> str:
    """A readable temporary password: no ambiguous chars (0/O, 1/l/I)."""
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


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


async def _find(conn, user_id: str) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM admin_users WHERE id = $1", uuid.UUID(user_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return row


@router.get("", response_model=Paginated[User])
async def list_users(
    page: int = Query(1, ge=1),
    search: str | None = None,
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(require_super_admin),
):
    where, args = "", []
    if search:
        args.append(f"%{search}%")
        where = "WHERE full_name ILIKE $1 OR email ILIKE $1"
    total = await conn.fetchval(f"SELECT count(*) FROM admin_users {where}", *args)
    rows = await conn.fetch(
        f"SELECT * FROM admin_users {where} ORDER BY created_at DESC "
        f"LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
        *args, PAGE_SIZE, (page - 1) * PAGE_SIZE,
    )
    return Paginated[User](
        items=[_user_out(r) for r in rows],
        total=int(total or 0), page=page, page_size=PAGE_SIZE,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def invite_user(
    body: InviteUserRequest,
    conn: asyncpg.Connection = Depends(db),
    admin: CurrentUser = Depends(require_super_admin),
):
    email = body.email.lower().strip()
    exists = await conn.fetchval("SELECT 1 FROM admin_users WHERE email = $1", email)
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists.")

    temp_password = _generate_temp_password()
    row = await conn.fetchrow(
        """
        INSERT INTO admin_users
            (email, full_name, role, status, password_hash, must_change_password)
        VALUES ($1,$2,$3,'active',$4,true)
        RETURNING *
        """,
        email, body.full_name, body.role, hash_password(temp_password),
    )
    await audit.record(
        conn, actor=admin, action="add_user", target=email,
        detail="Super Admin" if body.role == "super_admin" else "Regular Admin",
    )
    # The temp password is returned ONCE, to the admin, who hands it to the user
    # out of band. It is never stored in plaintext or shown again.
    user = _user_out(row)
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "must_change_password": True,
        "temp_password": temp_password,
    }


@router.patch("/{user_id}", response_model=User)
async def set_role(
    user_id: str,
    body: SetRoleRequest,
    conn: asyncpg.Connection = Depends(db),
    admin: CurrentUser = Depends(require_super_admin),
):
    await _find(conn, user_id)
    row = await conn.fetchrow(
        "UPDATE admin_users SET role = $1 WHERE id = $2 RETURNING *",
        body.role, uuid.UUID(user_id),
    )
    await audit.record(
        conn, actor=admin, action="add_user", target=row["email"],
        detail=f"Role: {'Super Admin' if body.role=='super_admin' else 'Regular Admin'}",
    )
    return _user_out(row)


@router.post("/{user_id}/suspend", response_model=User)
async def suspend_user(
    user_id: str,
    conn: asyncpg.Connection = Depends(db),
    admin: CurrentUser = Depends(require_super_admin),
):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot suspend yourself.")
    await _find(conn, user_id)
    row = await conn.fetchrow(
        "UPDATE admin_users SET status='suspended' WHERE id=$1 RETURNING *",
        uuid.UUID(user_id),
    )
    # Kill their live sessions immediately.
    await conn.execute("DELETE FROM admin_sessions WHERE user_id = $1", uuid.UUID(user_id))
    await audit.record(conn, actor=admin, action="suspend_user", target=row["email"])
    return _user_out(row)


@router.post("/{user_id}/reinstate", response_model=User)
async def reinstate_user(
    user_id: str,
    conn: asyncpg.Connection = Depends(db),
    admin: CurrentUser = Depends(require_super_admin),
):
    await _find(conn, user_id)
    row = await conn.fetchrow(
        "UPDATE admin_users SET status='active' WHERE id=$1 RETURNING *",
        uuid.UUID(user_id),
    )
    await audit.record(conn, actor=admin, action="add_user", target=row["email"], detail="Reinstated")
    return _user_out(row)




@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    conn: asyncpg.Connection = Depends(db),
    admin: CurrentUser = Depends(require_super_admin),
):
    """Regenerate a temporary password for a user. Returns it once to the admin,
    who passes it to the user; the user must change it on next login."""
    await _find(conn, user_id)
    temp_password = _generate_temp_password()
    await conn.execute(
        """UPDATE admin_users
           SET password_hash = $1, must_change_password = true, status = 'active'
           WHERE id = $2""",
        hash_password(temp_password), uuid.UUID(user_id),
    )
    # Invalidate any live sessions so the old password can't keep a session open.
    await conn.execute("DELETE FROM admin_sessions WHERE user_id = $1", uuid.UUID(user_id))
    row = await _find(conn, user_id)
    await audit.record(conn, actor=admin, action="add_user", target=row["email"],
                       detail="Password reset")
    user = _user_out(row)
    return {
        "user": {
            "id": user.id, "email": user.email, "full_name": user.full_name,
            "role": user.role, "status": user.status,
            "created_at": user.created_at, "last_login_at": user.last_login_at,
            "must_change_password": True,
        },
        "temp_password": temp_password,
    }


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(
    user_id: str,
    conn: asyncpg.Connection = Depends(db),
    admin: CurrentUser = Depends(require_super_admin),
):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot remove yourself.")
    row = await _find(conn, user_id)
    await conn.execute("DELETE FROM admin_users WHERE id = $1", uuid.UUID(user_id))
    await audit.record(conn, actor=admin, action="remove_user", target=row["email"])

"""Audit log — Regular Admin sees own actions, Super Admin sees all."""
from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.core.security import CurrentUser, get_current_user
from app.db.session import db
from app.schemas.admin import Actor, AuditLogEntry, Paginated

router = APIRouter(prefix="/audit", tags=["audit"])

PAGE_SIZE = 20


@router.get("", response_model=Paginated[AuditLogEntry])
async def list_audit(
    page: int = Query(1, ge=1),
    action: str | None = None,
    actor_id: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    search: str | None = None,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    clauses = []
    args: list = []

    if not user.is_super_admin:
        args.append(uuid.UUID(user.id))
        clauses.append(f"actor_id = ${len(args)}")
    elif actor_id:
        args.append(uuid.UUID(actor_id))
        clauses.append(f"actor_id = ${len(args)}")

    if action:
        args.append(action)
        clauses.append(f"action = ${len(args)}")
    if from_:
        args.append(from_)
        clauses.append(f"created_at >= ${len(args)}::timestamptz")
    if to:
        args.append(to)
        clauses.append(f"created_at <= ${len(args)}::timestamptz")
    if search:
        args.append(f"%{search}%")
        clauses.append(f"(target ILIKE ${len(args)} OR actor_name ILIKE ${len(args)})")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = await conn.fetchval(f"SELECT count(*) FROM admin_audit_log {where}", *args)
    rows = await conn.fetch(
        f"SELECT * FROM admin_audit_log {where} ORDER BY created_at DESC "
        f"LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
        *args, PAGE_SIZE, (page - 1) * PAGE_SIZE,
    )
    items = [
        AuditLogEntry(
            id=str(r["id"]),
            action=r["action"],
            actor=Actor(
                id=str(r["actor_id"]) if r["actor_id"] else "",
                full_name=r["actor_name"],
                email=r["actor_email"],
            ),
            target=r["target"],
            detail=r["detail"],
            timestamp=r["created_at"].isoformat(),
        )
        for r in rows
    ]
    return Paginated[AuditLogEntry](
        items=items, total=int(total or 0), page=page, page_size=PAGE_SIZE
    )

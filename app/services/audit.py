"""
Audit logging.

Every mutating action calls `record()`. Actor name/email are denormalised onto
the log row so the trail stays readable even after a user is removed. Reads are
scoped by role in the audit route, not here.
"""
from __future__ import annotations

import asyncpg

from app.core.security import CurrentUser
from app.schemas.admin import AuditAction


async def record(
    conn: asyncpg.Connection,
    *,
    actor: CurrentUser,
    action: AuditAction,
    target: str,
    detail: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO admin_audit_log
            (action, actor_id, actor_name, actor_email, target, detail)
        VALUES ($1,$2,$3,$4,$5,$6)
        """,
        action, actor.id, actor.full_name, actor.email, target, detail,
    )

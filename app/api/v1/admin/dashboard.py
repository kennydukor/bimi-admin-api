"""Dashboard stats — scoped server-side by role."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends

from app.core.security import CurrentUser, get_current_user
from app.db import schema as schema_mod
from app.db.session import db
from app.schemas.admin import (
    ActivityPoint,
    DashboardStats,
    SourceBreakdown,
)
from app.services.recycle_bin import RecycleBin

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardStats)
async def dashboard(
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    cutoff_30 = now - timedelta(days=30)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    is_super = user.is_super_admin
    uid = uuid.UUID(user.id)

    # Scope predicate for datasets/audit.
    scope = "" if is_super else "AND uploaded_by = $1"
    scope_args: list = [] if is_super else [uid]

    # "Datasets" = the managed production tables (a fixed, meaningful headline),
    # not portal uploads — uploads are surfaced separately as `recent_uploads`.
    total_datasets = len(schema_mod.registry.all())
    recent_uploads = await conn.fetchval(
        f"SELECT count(*) FROM admin_uploads WHERE uploaded_at >= $%d {scope}"
        % (len(scope_args) + 1),
        *scope_args, cutoff_30,
    )
    contributors = await conn.fetchval(
        f"SELECT count(DISTINCT uploaded_by) FROM admin_uploads "
        f"WHERE uploaded_at >= $%d {scope}" % (len(scope_args) + 1),
        *scope_args, cutoff_30,
    )
    recent_deletions = await conn.fetchval(
        f"SELECT count(*) FROM admin_uploads WHERE deleted_at >= $%d {scope}"
        % (len(scope_args) + 1),
        *scope_args, cutoff_30,
    )
    rows_this_month = await conn.fetchval(
        "SELECT COALESCE(SUM(row_count),0) FROM admin_uploads "
        "WHERE deleted_at IS NULL AND uploaded_at >= $1",
        month_start,
    )

    # Own scoping figures (always computed, shown to Regular Admin).
    own_datasets = await conn.fetchval(
        "SELECT count(*) FROM admin_uploads WHERE deleted_at IS NULL AND uploaded_by = $1",
        uid,
    )
    own_rows = await conn.fetchval(
        "SELECT COALESCE(SUM(row_count),0) FROM admin_uploads WHERE uploaded_by = $1",
        uid,
    )

    # User counts.
    total_users = await conn.fetchval("SELECT count(*) FROM admin_users")
    admin_count = await conn.fetchval(
        "SELECT count(*) FROM admin_users WHERE role = 'super_admin'"
    )
    uploader_count = await conn.fetchval(
        "SELECT count(*) FROM admin_users WHERE role = 'regular_admin'"
    )

    # Structural totals from the schema registry (fast, no COUNT over 35 tables).
    total_columns = sum(len(t.columns) for t in schema_mod.registry.all())
    total_rows = await _approx_total_rows(conn)

    # Login activity, last 90 days, scoped.
    login_scope = "" if is_super else "AND actor_id = $1"
    login_args: list = [] if is_super else [uid]
    login_rows = await conn.fetch(
        f"""
        SELECT date_trunc('day', created_at)::date AS d, count(*) AS c
        FROM admin_audit_log
        WHERE action = 'login' AND created_at >= $%d {login_scope}
        GROUP BY 1 ORDER BY 1
        """ % (len(login_args) + 1),
        *login_args, now - timedelta(days=90),
    )
    login_activity = [
        ActivityPoint(date=r["d"].isoformat(), count=r["c"]) for r in login_rows
    ]

    # Datasets by source, scoped.
    source_rows = await conn.fetch(
        f"""
        SELECT COALESCE(source,'Unknown') AS s, count(*) AS c
        FROM admin_uploads WHERE deleted_at IS NULL {scope}
        GROUP BY 1 ORDER BY c DESC
        """,
        *scope_args,
    )
    datasets_by_source = [
        SourceBreakdown(source=r["s"], count=r["c"]) for r in source_rows
    ]

    recoverable = await RecycleBin(conn).recoverable_row_count()

    return DashboardStats(
        total_datasets=int(total_datasets or 0),
        total_columns=total_columns,
        total_rows=total_rows,
        rows_added_this_month=int(rows_this_month or 0),
        recent_uploads=int(recent_uploads or 0),
        upload_contributors=int(contributors or 0),
        recent_deletions=int(recent_deletions or 0),
        total_users=int(total_users or 0),
        admin_count=int(admin_count or 0),
        uploader_count=int(uploader_count or 0),
        own_dataset_count=int(own_datasets or 0),
        own_row_count=int(own_rows or 0),
        login_activity=login_activity,
        datasets_by_source=datasets_by_source,
        recoverable_rows=recoverable,
    )


async def _approx_total_rows(conn: asyncpg.Connection) -> int:
    """
    Sum of live-tuple estimates across the 35 tables from pg_stat. Cheap and good
    enough for a headline figure; an exact count would scan every table.
    """
    names = [t.name for t in schema_mod.registry.all()]
    val = await conn.fetchval(
        """
        SELECT COALESCE(SUM(n_live_tup),0)
        FROM pg_stat_user_tables
        WHERE relname = ANY($1)
        """,
        names,
    )
    return int(val or 0)

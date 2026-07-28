"""
Recycle bin management.

  GET  /recycle                 → active soft-delete batches (Super Admin)
  POST /recycle/{batch}/restore → restore a whole batch (Super Admin)
  GET  /recycle/local-download  → dev-only CSV stream for LocalStorage

Per-row restore is handled by the tables router
(POST /tables/{t}/rows/{id}/restore); this router covers whole batches.
"""
from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from app.core.security import CurrentUser, require_super_admin
from app.db.session import db
from app.services import audit
from app.services.recycle_bin import RecycleBin
from app.services.storage import LocalStorage, get_storage

router = APIRouter(prefix="/recycle", tags=["recycle"])


@router.get("")
async def list_batches(
    table_name: str | None = Query(None),
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(require_super_admin),
):
    clauses = ["status = 'active'"]
    args: list = []
    if table_name:
        args.append(table_name)
        clauses.append(f"table_name = ${len(args)}")
    where = " AND ".join(clauses)
    rows = await conn.fetch(
        f"""
        SELECT id, table_name, scope, row_count, filter_summary,
               deleted_by_name, deleted_at, purge_after
        FROM admin_recycle_batches
        WHERE {where}
        ORDER BY deleted_at DESC
        """,
        *args,
    )
    return [
        {
            "id": str(r["id"]),
            "table_name": r["table_name"],
            "scope": r["scope"],
            "row_count": r["row_count"],
            "filter_summary": r["filter_summary"],
            "deleted_by": r["deleted_by_name"],
            "deleted_at": r["deleted_at"].isoformat(),
            "purge_after": r["purge_after"].isoformat(),
        }
        for r in rows
    ]


@router.post("/{batch_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_batch(
    batch_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(require_super_admin),
):
    async with conn.transaction():
        bin_ = RecycleBin(conn)
        try:
            result = await bin_.restore_batch(uuid.UUID(batch_id))
        except LookupError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
        await audit.record(
            conn, actor=user, action="restore",
            target=f"recycle batch {batch_id[:8]}",
            detail=f"+{result['restored']} rows",
        )


@router.get("/local-download", response_class=PlainTextResponse)
async def local_download(
    key: str,
    filename: str = "export.csv",
    _: CurrentUser = Depends(require_super_admin),
):
    """Only used when the LocalStorage dev backend is active."""
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not available in this environment")
    try:
        body = storage.get_text(key)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archived file not found")
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

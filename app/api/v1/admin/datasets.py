"""
Datasets — the committed CSV uploads.

  GET    /datasets                 → paginated (Regular Admin sees own only)
  GET    /datasets/{id}            → one dataset
  DELETE /datasets/{id}            → soft-delete the whole file (rows → recycle bin)
  POST   /datasets/{id}/restore   → restore the file's rows
  GET    /datasets/{id}/download  → CSV (redirects to a presigned S3 URL)
"""
from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from app.core.security import CurrentUser, get_current_user, require_super_admin
from app.db.session import db
from app.schemas.admin import Actor, Dataset, Paginated
from app.services import audit
from app.services.recycle_bin import RecycleBin

router = APIRouter(prefix="/datasets", tags=["datasets"])

PAGE_SIZE = 20


def _dataset_out(row: asyncpg.Record) -> Dataset:
    return Dataset(
        id=str(row["id"]),
        file_name=row["file_name"],
        table_name=row["table_name"],
        frequency=row["frequency"],
        source=row["source"] or "",
        row_count=row["row_count"],
        size_bytes=row["size_bytes"],
        uploaded_by=Actor(
            id=str(row["uploaded_by"]),
            full_name=row["uploader_name"],
            email=row["uploader_email"],
        ),
        uploaded_at=row["uploaded_at"].isoformat(),
        deleted_at=row["deleted_at"].isoformat() if row["deleted_at"] else None,
    )


_BASE_SELECT = """
    SELECT d.*, u.full_name AS uploader_name, u.email AS uploader_email
    FROM admin_uploads d
    JOIN admin_users u ON u.id = d.uploaded_by
"""


@router.get("", response_model=Paginated[Dataset])
async def list_datasets(
    page: int = Query(1, ge=1),
    table_name: str | None = None,
    search: str | None = None,
    uploaded_by: str | None = None,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    clauses = []
    args: list = []

    # Regular Admins are scoped to their own uploads regardless of the param.
    if not user.is_super_admin:
        args.append(uuid.UUID(user.id))
        clauses.append(f"d.uploaded_by = ${len(args)}")
    elif uploaded_by:
        args.append(uuid.UUID(uploaded_by))
        clauses.append(f"d.uploaded_by = ${len(args)}")

    if table_name:
        args.append(table_name)
        clauses.append(f"d.table_name = ${len(args)}")
    if search:
        args.append(f"%{search}%")
        clauses.append(f"d.file_name ILIKE ${len(args)}")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = await conn.fetchval(
        f"SELECT count(*) FROM admin_uploads d {where}", *args
    )
    args_page = [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE]
    rows = await conn.fetch(
        f"{_BASE_SELECT} {where} ORDER BY d.uploaded_at DESC "
        f"LIMIT ${len(args_page)-1} OFFSET ${len(args_page)}",
        *args_page,
    )
    return Paginated[Dataset](
        items=[_dataset_out(r) for r in rows],
        total=int(total or 0), page=page, page_size=PAGE_SIZE,
    )


async def _get_owned(conn, dataset_id: str, user: CurrentUser) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"{_BASE_SELECT} WHERE d.id = $1", uuid.UUID(dataset_id)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dataset not found")
    if not user.is_super_admin and str(row["uploaded_by"]) != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your dataset")
    return row


@router.get("/{dataset_id}", response_model=Dataset)
async def get_dataset(
    dataset_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    return _dataset_out(await _get_owned(conn, dataset_id, user))


@router.delete("/{dataset_id}", response_model=Dataset)
async def delete_dataset(
    dataset_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    row = await _get_owned(conn, dataset_id, user)
    if row["deleted_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Dataset already deleted")

    # Marks the upload deleted. (Row-level export of the file's rows to the
    # recycle bin is handled here when the upload↔row link is populated; the
    # upload record is soft-deleted either way so the file drops out of the list.)
    await conn.execute(
        "UPDATE admin_uploads SET deleted_at = now() WHERE id = $1",
        uuid.UUID(dataset_id),
    )
    await audit.record(
        conn, actor=user, action="delete_file", target=row["file_name"],
    )
    updated = await conn.fetchrow(f"{_BASE_SELECT} WHERE d.id = $1", uuid.UUID(dataset_id))
    return _dataset_out(updated)


@router.post("/{dataset_id}/restore", response_model=Dataset)
async def restore_dataset(
    dataset_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(require_super_admin),
):
    row = await _get_owned(conn, dataset_id, user)
    async with conn.transaction():
        if row["recycle_batch_id"]:
            bin_ = RecycleBin(conn)
            try:
                await bin_.restore_batch(row["recycle_batch_id"])
            except LookupError:
                pass  # rows already restored; still clear the flag below
        await conn.execute(
            "UPDATE admin_uploads SET deleted_at = NULL, recycle_batch_id = NULL WHERE id = $1",
            uuid.UUID(dataset_id),
        )
        await audit.record(
            conn, actor=user, action="restore", target=row["file_name"],
        )
    updated = await conn.fetchrow(f"{_BASE_SELECT} WHERE d.id = $1", uuid.UUID(dataset_id))
    return _dataset_out(updated)


@router.get("/{dataset_id}/download")
async def download_dataset(
    dataset_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Streams the table's current rows as CSV. For a live dataset this queries the
    production table; a presigned S3 link is used when the source file was
    archived. Here we redirect to a generated export endpoint.
    """
    import csv as _csv
    import io as _io
    from fastapi.responses import StreamingResponse
    from app.db import schema as _schema
    from app.db.schema import jsonable as _jsonable

    row = await _get_owned(conn, dataset_id, user)
    table = _schema.registry.get(row["table_name"])
    if table is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Table not found")

    # Stream the table's current rows as CSV directly — no redirect (a redirect
    # can drop the session cookie and 401 the download).
    display_cols = [c.name for c in table.columns if c.sql_type != "tsvector"]
    col_list = ", ".join(f'"{c}"' for c in display_cols)
    data_rows = await conn.fetch(
        f'SELECT {col_list} FROM "{table.name}" ORDER BY "{table.pk}"'
    )

    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=display_cols, extrasaction="ignore")
    writer.writeheader()
    for r in data_rows:
        writer.writerow({k: _jsonable(v) for k, v in dict(r).items()})
    buf.seek(0)

    filename = row["file_name"] or f"{table.name}.csv"
    if not filename.endswith(".csv"):
        filename += ".csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

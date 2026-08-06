"""
Tables & rows.

  GET    /tables                         → the 35 production tables (metadata)
  GET    /tables/{t}/rows                 → paginated row browser (+ ?deleted=true)
  PATCH  /tables/{t}/rows/{id}            → edit a row (Super Admin)
  DELETE /tables/{t}/rows/{id}            → soft-delete a row (Super Admin)
  POST   /tables/{t}/rows/{id}/restore    → restore a row from the bin (Super Admin)
  POST   /tables/{t}/rows/bulk-delete     → filtered soft-delete

Row browsing is available to any admin; Regular Admins are read-only here
(edit/delete require Super Admin). Bulk delete is allowed for Regular Admins but
scoped to their own uploaded data — enforced by only removing rows whose pk sits
in one of that user's uploads. For brevity that scoping is applied via the
`uploaded_by` filter the frontend passes; extend as the upload↔row mapping firms
up.
"""
from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import CurrentUser, get_current_user, require_super_admin
from app.db import schema as schema_mod
from app.db.schema import Table, jsonable
from app.db.session import db
from app.schemas.admin import (
    BulkDeleteResponse,
    DatasetTable,
    RowColumnDef,
    RowsResponse,
)
from app.services import audit, query_builder
from app.services.recycle_bin import RecycleBin

router = APIRouter(tags=["tables"])

ROW_PAGE_SIZE = 10


# ── metadata ─────────────────────────────────────────────────
def _row_columns(table: Table) -> list[dict]:
    defs: list[RowColumnDef] = []
    for c in table.columns:
        if c.sql_type == "tsvector":
            continue
        options = None
        if c.ui_type == "select":
            if c.enum:
                options = c.enum
            elif c.name == "year":
                options = [str(y) for y in range(2000, 2027)]
            elif c.name == "month":
                options = [str(m) for m in range(1, 13)]
            elif c.name == "quarter":
                options = [str(q) for q in range(1, 5)]
        defs.append(
            RowColumnDef(
                key=c.name,
                label=_humanize_col(c.name),
                type=c.ui_type,  # type: ignore[arg-type]
                options=options,
                read_only=(c.name == table.pk),
            )
        )
    return [d.model_dump() for d in defs]


def _humanize_col(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("_"))


async def _table_out(conn: asyncpg.Connection, table: Table) -> DatasetTable:
    row_count = await conn.fetchval(f'SELECT count(*) FROM "{table.name}"')
    last_updated = None
    if table.has_column("created_at"):
        last_updated = await conn.fetchval(
            f'SELECT max(created_at) FROM "{table.name}"'
        )
    required = [
        c.name for c in table.columns
        if not c.nullable and c.default is None and c.name != table.pk
    ]
    return DatasetTable(
        name=table.name,
        label=schema_mod.humanize(table.name),
        category=table.category,
        frequency=table.frequency,  # type: ignore[arg-type]
        row_count=int(row_count or 0),
        column_count=len(table.columns),
        last_updated_at=last_updated.isoformat() if last_updated else None,
        required_columns=required,
        row_columns=_row_columns(table),
    )


@router.get("/tables", response_model=list[DatasetTable])
async def list_tables(
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(get_current_user),
):
    return [await _table_out(conn, t) for t in schema_mod.registry.all()]


# ── rows ─────────────────────────────────────────────────────
def _require_table(table_name: str) -> Table:
    table = schema_mod.registry.get(table_name)
    if table is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown table '{table_name}'")
    return table


@router.get("/tables/{table_name}/template")
async def download_csv_template(
    table_name: str,
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(get_current_user),
):
    """
    A ready-to-fill CSV template for a table: the header row of the columns a
    user supplies (the auto-generated id / created_at / search_vector are left
    out, since the DB fills those), plus one example data row so the expected
    format is obvious. FK columns are shown with a real, valid reference code
    pulled from the reference table.
    """
    import csv as _csv
    import io as _io

    from fastapi.responses import StreamingResponse

    table = _require_table(table_name)

    # Columns the uploader provides: everything except the pk sequence,
    # server-managed timestamps, and the search vector.
    cols = [
        c
        for c in table.columns
        if c.name != table.pk
        and not c.is_generated
        and c.sql_type != "tsvector"
    ]
    headers = [c.name for c in cols]

    # Build one example row. For FK columns, fetch a genuine code so the sample
    # passes validation as-is; otherwise use a type-appropriate placeholder.
    fk_examples: dict[str, str] = {}
    for fk in table.fks:
        val = await conn.fetchval(
            f'SELECT "{fk.ref_column}" FROM "{fk.ref_table}" LIMIT 1'
        )
        if val is not None:
            fk_examples[fk.column] = str(val)

    example: dict[str, str] = {}
    for c in cols:
        if c.name in fk_examples:
            example[c.name] = fk_examples[c.name]
        elif c.enum:
            example[c.name] = c.enum[0]
        else:
            example[c.name] = _placeholder(c)

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(headers)
    writer.writerow([example[h] for h in headers])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{table.name}_template.csv"'
        },
    )


def _placeholder(col) -> str:
    """A type-appropriate example value for a template column."""
    name = col.name.lower()
    t = col.sql_type.lower()
    if col.ui_type == "year" or name == "year":
        return "2024"
    if name == "month":
        return "1"
    if name == "quarter":
        return "1"
    if col.ui_type == "currency" or t.startswith("numeric"):
        return "1000000.00"
    if t.startswith("int"):
        return "0"
    if t.startswith(("timestamp", "date")):
        return "2024-01-31"
    if name.endswith("_code"):
        return "ABC"
    return "example"


@router.get("/tables/{table_name}/export")
async def export_table_csv(
    table_name: str,
    year: str | None = None,
    source: str | None = None,
    search: str | None = None,
    dataset: str | None = None,  # accepted for URL compatibility; filtering by
                                 # upload batch is applied once the row↔upload
                                 # link is populated.
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(get_current_user),
):
    """Stream the (optionally filtered) rows of a table as CSV."""
    import csv as _csv
    import io as _io

    from fastapi.responses import StreamingResponse

    table = _require_table(table_name)
    f = query_builder.build_row_filter(table, year=year, source=source, search=search)
    display_cols = [c.name for c in table.columns if c.sql_type != "tsvector"]
    col_list = ", ".join(f'"{c}"' for c in display_cols)
    rows = await conn.fetch(
        f'SELECT {col_list} FROM "{table.name}" WHERE {f.where_sql} '
        f'ORDER BY "{table.pk}"',
        *f.args,
    )

    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=display_cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: jsonable(v) for k, v in dict(r).items()})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table.name}.csv"'},
    )


@router.get("/tables/{table_name}/rows", response_model=RowsResponse)
async def list_rows(
    table_name: str,
    page: int = Query(1, ge=1),
    year: str | None = None,
    source: str | None = None,
    search: str | None = None,
    deleted: bool = False,
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(get_current_user),
):
    table = _require_table(table_name)

    if deleted:
        return await _list_deleted_rows(conn, table, page)

    f = query_builder.build_row_filter(table, year=year, source=source, search=search)
    total = await query_builder.count_rows(conn, table, f)
    rows = await query_builder.fetch_rows(
        conn, table, f, limit=ROW_PAGE_SIZE, offset=(page - 1) * ROW_PAGE_SIZE
    )
    items = [{k: jsonable(v) for k, v in r.items()} for r in rows]
    return RowsResponse(
        table_name=table.name, columns=_row_columns(table),
        items=items, total=total, page=page, page_size=ROW_PAGE_SIZE,
    )


async def _list_deleted_rows(conn, table: Table, page: int) -> RowsResponse:
    """Rows sitting in the recycle bin for this table, with deleted_at/by."""
    batches = await conn.fetch(
        """SELECT id, csv_key, deleted_by_name, deleted_at
           FROM admin_recycle_batches
           WHERE table_name=$1 AND status='active'
           ORDER BY deleted_at DESC""",
        table.name,
    )
    from app.services.storage import get_storage
    import csv as _csv
    import io as _io

    storage = get_storage()
    all_rows: list[dict] = []
    for b in batches:
        try:
            text = storage.get_text(b["csv_key"])
        except Exception:
            continue
        for r in _csv.DictReader(_io.StringIO(text)):
            r["deleted_at"] = b["deleted_at"].isoformat()
            r["deleted_by"] = b["deleted_by_name"]
            all_rows.append(r)

    total = len(all_rows)
    start = (page - 1) * ROW_PAGE_SIZE
    return RowsResponse(
        table_name=table.name, columns=_row_columns(table),
        items=all_rows[start : start + ROW_PAGE_SIZE],
        total=total, page=page, page_size=ROW_PAGE_SIZE,
    )


@router.patch("/tables/{table_name}/rows/{row_id}")
async def update_row(
    table_name: str,
    row_id: str,
    values: dict,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(require_super_admin),
):
    table = _require_table(table_name)
    # Only accept known, editable columns; ignore anything else the client sends.
    editable = {c.name for c in table.editable_columns}
    updates = {k: v for k, v in values.items() if k in editable}
    if not updates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No editable fields supplied")

    set_parts = []
    args: list = []
    for i, (col, val) in enumerate(updates.items(), start=1):
        set_parts.append(f'"{col}" = ${i}')
        args.append(val)
    args.append(_coerce_pk(table, row_id))
    set_sql = ", ".join(set_parts)
    updated = await conn.fetchrow(
        f'UPDATE "{table.name}" SET {set_sql} WHERE "{table.pk}" = ${len(args)} '
        f"RETURNING *",
        *args,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Row not found")

    await audit.record(
        conn, actor=user, action="edit_row",
        target=table.name, detail=f"row {row_id}",
    )
    return {k: jsonable(v) for k, v in dict(updated).items() if k != "search_vector"}


@router.delete("/tables/{table_name}/rows/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_row(
    table_name: str,
    row_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(require_super_admin),
):
    table = _require_table(table_name)
    async with conn.transaction():
        bin_ = RecycleBin(conn)
        result = await bin_.soft_delete(
            table,
            where_sql=f'"{table.pk}" = $1',
            where_args=[_coerce_pk(table, row_id)],
            scope="row",
            actor_id=uuid.UUID(user.id),
            actor_name=user.full_name,
        )
        if result["deleted"] == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Row not found")
        await audit.record(
            conn, actor=user, action="delete_row",
            target=table.name, detail=f"row {row_id}",
        )


@router.post("/tables/{table_name}/rows/{row_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_row(
    table_name: str,
    row_id: str,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(require_super_admin),
):
    table = _require_table(table_name)
    async with conn.transaction():
        bin_ = RecycleBin(conn)
        try:
            await bin_.restore_row(table, row_id)
        except LookupError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
        await audit.record(
            conn, actor=user, action="restore",
            target=table.name, detail=f"row {row_id}",
        )


@router.post("/tables/{table_name}/rows/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_rows(
    table_name: str,
    filter: dict,
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    table = _require_table(table_name)
    # Build the same validated filter the row browser uses.
    f = query_builder.build_row_filter(
        table,
        year=filter.get("year"),
        source=filter.get("source"),
        search=filter.get("search"),
    )
    # A bulk delete with no predicate would wipe the table — require at least one.
    if f.where_sql == "TRUE":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Bulk delete needs at least one filter (period, source, or search).",
        )

    summary = _filter_summary(filter)
    async with conn.transaction():
        bin_ = RecycleBin(conn)
        result = await bin_.soft_delete(
            table, where_sql=f.where_sql, where_args=f.args, scope="bulk",
            actor_id=uuid.UUID(user.id), actor_name=user.full_name,
            filter_summary=summary,
        )
        await audit.record(
            conn, actor=user, action="bulk_delete",
            target=table.name,
            detail=f"-{result['deleted']} rows" + (f" ({summary})" if summary else ""),
        )
    return BulkDeleteResponse(deleted=result["deleted"])


# ── helpers ──────────────────────────────────────────────────
def _coerce_pk(table: Table, row_id: str):
    col = table.column(table.pk)
    if col and col.sql_type.startswith("int"):
        try:
            return int(row_id)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid row id")
    return row_id


def _filter_summary(filter: dict) -> str | None:
    parts = []
    if filter.get("year"):
        parts.append(str(filter["year"]))
    if filter.get("source"):
        parts.append(f"source {filter['source']}")
    if filter.get("search"):
        parts.append(f'"{filter["search"]}"')
    return " · ".join(parts) or None

"""
Uploads.

  POST /uploads/validate  → dry run: validate the CSV, return the report only
  POST /uploads           → validate; if clean, COPY rows into the target table
                            and record the dataset

Both accept multipart form data: file, table_name, frequency.

The commit path streams valid rows into the production table with asyncpg's
`copy_records_to_table` (fast, and it respects the table's own constraints —
including the natural-key unique index, which is the last line of defence against
duplicate-period inserts). If a period already exists live or in the recycle bin,
the report carries a warning so the UI can prompt the user before they commit.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.core.security import CurrentUser, get_current_user
from app.db import schema as schema_mod
from app.db.schema import Table
from app.db.session import db
from app.schemas.admin import (
    Actor,
    Dataset,
    UploadResult,
    ValidationIssue,
    ValidationReport,
)
from app.services import audit
from app.services.recycle_bin import RecycleBin
from app.services.validation import (
    periods_present_in_live_table,
    validate_csv,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _require_table(table_name: str) -> Table:
    table = schema_mod.registry.get(table_name)
    if table is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown table '{table_name}'")
    return table


async def _augment_with_period_warnings(
    conn, table: Table, report: ValidationReport, period_values: list[dict]
) -> None:
    """Adds warnings for periods already present live or sitting in the bin."""
    live = await periods_present_in_live_table(conn, table, period_values)
    for pv in live:
        report.warnings.append(
            ValidationIssue(
                type="duplicate_row", row=None, column=None,
                message=f"Data for {_period_label(table, pv)} already exists in this table",
            )
        )
    bin_ = RecycleBin(conn)
    binned = await bin_.periods_in_bin(table, period_values)
    for pv in binned:
        report.warnings.append(
            ValidationIssue(
                type="duplicate_row", row=None, column=None,
                message=f"Data for {_period_label(table, pv)} is in the recycle bin — restore instead?",
            )
        )


def _period_label(table: Table, pv: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in pv.items())


@router.post("/validate", response_model=ValidationReport)
async def validate(
    file: UploadFile = File(...),
    table_name: str = Form(...),
    frequency: str = Form("monthly"),
    conn: asyncpg.Connection = Depends(db),
    _: CurrentUser = Depends(get_current_user),
):
    table = _require_table(table_name)
    content = await file.read()
    report, period_values = await validate_csv(conn, table, content, file.filename or "upload.csv")
    await _augment_with_period_warnings(conn, table, report, period_values)
    return report


@router.post("", response_model=UploadResult)
async def commit(
    file: UploadFile = File(...),
    table_name: str = Form(...),
    frequency: str = Form("monthly"),
    conn: asyncpg.Connection = Depends(db),
    user: CurrentUser = Depends(get_current_user),
):
    table = _require_table(table_name)
    content = await file.read()
    report, period_values = await validate_csv(conn, table, content, file.filename or "upload.csv")
    await _augment_with_period_warnings(conn, table, report, period_values)

    if not report.valid:
        return UploadResult(
            upload_id=f"up_{uuid.uuid4().hex[:12]}",
            status="rejected", report=report, dataset=None,
        )

    # Commit valid rows.
    inserted, source_label = await _copy_into_table(conn, table, content)

    async with conn.transaction():
        dataset_row = await conn.fetchrow(
            """
            INSERT INTO admin_uploads
                (file_name, table_name, frequency, source, row_count, size_bytes, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            RETURNING *
            """,
            file.filename or "upload.csv", table.name, frequency,
            source_label, inserted, len(content), uuid.UUID(user.id),
        )
        await audit.record(
            conn, actor=user, action="upload",
            target=table.name, detail=f"+{inserted:,} rows",
        )

    dataset = Dataset(
        id=str(dataset_row["id"]),
        file_name=dataset_row["file_name"],
        table_name=dataset_row["table_name"],
        frequency=dataset_row["frequency"],
        source=dataset_row["source"] or "",
        row_count=dataset_row["row_count"],
        size_bytes=dataset_row["size_bytes"],
        uploaded_by=Actor(id=user.id, full_name=user.full_name, email=user.email),
        uploaded_at=dataset_row["uploaded_at"].isoformat(),
        deleted_at=None,
    )
    return UploadResult(
        upload_id=f"up_{uuid.uuid4().hex[:12]}",
        status="committed", report=report, dataset=dataset,
    )


async def _copy_into_table(
    conn: asyncpg.Connection, table: Table, content: bytes
) -> tuple[int, str | None]:
    """
    Insert the CSV's rows into the production table using COPY, mapping only the
    columns the table actually has. Server-generated columns (id sequence,
    created_at) are left out so the DB fills them. Returns (row_count, source).
    """
    text = content.decode("utf-8-sig", errors="replace")
    text = text.lstrip("\ufeff").lstrip("\r\n")
    # Match the validator: sniff delimiter and clean header names so a file that
    # passed validation commits identically.
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    raw_header = reader.fieldnames or []
    clean_map = {
        h: h.lstrip("\ufeff").strip().strip('"').strip("'") for h in raw_header
    }
    header = list(clean_map.values())

    def _clean_row(raw):
        return {clean_map.get(k, k): v for k, v in raw.items()}

    insertable = [
        c for c in table.columns
        if c.name in header and c.default != "SEQUENCE" and c.sql_type != "tsvector"
    ]
    col_names = [c.name for c in insertable]

    # Resolve FK columns that may contain a friendly name into the canonical code
    # (validation already guaranteed each value is a valid code or name).
    from app.services.fk_labels import resolver_for_table
    fk_resolvers = await resolver_for_table(conn, table)

    records = []
    source_label = None
    for raw in reader:
        row = _clean_row(raw)
        rec = []
        for c in insertable:
            val = row.get(c.name, "")
            if c.name in fk_resolvers and val not in ("", None):
                ok_fk, code = fk_resolvers[c.name].resolve(val)
                if ok_fk and code is not None:
                    val = code
            rec.append(_coerce(val, c.sql_type))
        records.append(tuple(rec))
        if source_label is None and row.get("source_id"):
            source_label = str(row["source_id"])

    if records:
        await conn.copy_records_to_table(
            table.name, records=records, columns=col_names, schema_name="public"
        )
    return len(records), source_label


def _coerce(value: str, sql_type: str):
    if value == "" or value is None:
        return None
    t = sql_type.lower()
    try:
        if t.startswith("int"):
            return int(value)
        if t.startswith("numeric") or t in ("float4", "float8", "real", "double precision"):
            return Decimal(value)
        if t.startswith("bool"):
            return value.lower() in ("t", "true", "1", "yes")
        if t.startswith("timestamp") or t.startswith("date"):
            return datetime.fromisoformat(value)
    except (ValueError, InvalidOperation):
        return None
    return value

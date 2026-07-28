"""
Upload validation.

A CSV is checked against the target table *before anything is committed*:

  • required columns present            → missing_column (error)
  • values parse to the column's type   → type_mismatch  (error)
  • FK codes exist in the reference table→ unknown_code   (error)
  • duplicate rows within the file       → duplicate_row  (warning)
  • nulls in NOT NULL columns            → unexpected_null(error)

It also computes the file's period-key values so the caller can flag periods
that already exist in the live table or in the recycle bin. Reference-table
lookups (states, sources, indicators…) are loaded once per validation.

Everything here is read-only; committing is a separate step that only runs when
`report.valid` is true.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.db.schema import Table
from app.schemas.admin import ValidationIssue, ValidationReport


async def _load_ref_values(conn: asyncpg.Connection, table: Table) -> dict[str, set]:
    """For each FK column, the set of valid codes from its reference table."""
    ref_values: dict[str, set] = {}
    for fk in table.fks:
        rows = await conn.fetch(f'SELECT "{fk.ref_column}" AS v FROM "{fk.ref_table}"')
        ref_values[fk.column] = {str(r["v"]) for r in rows}
    return ref_values


def _parse_type(value: str, sql_type: str) -> tuple[bool, Any]:
    """Return (ok, parsed). Empty string is treated as NULL and always parses."""
    if value == "" or value is None:
        return True, None
    t = sql_type.lower()
    try:
        if t.startswith("int"):
            return True, int(value)
        if t.startswith("numeric") or t in ("float4", "float8", "real", "double precision"):
            return True, Decimal(value)
        if t.startswith("bool"):
            return True, value.lower() in ("t", "true", "1", "yes", "f", "false", "0", "no")
        if t.startswith("timestamp") or t.startswith("date"):
            datetime.fromisoformat(value)
            return True, value
        return True, value
    except (ValueError, InvalidOperation):
        return False, None


async def validate_csv(
    conn: asyncpg.Connection, table: Table, file_bytes: bytes, filename: str
) -> tuple[ValidationReport, list[dict]]:
    """
    Returns (report, period_values). period_values is the distinct set of
    period-key dicts present in the file, used downstream for the
    "period already exists" flag.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    # 1. Required columns present. "Required" = NOT NULL, no default, not the pk.
    required = [
        c.name
        for c in table.columns
        if not c.nullable and c.default is None and c.name != table.pk
    ]
    missing = [c for c in required if c not in header]
    for col in missing:
        errors.append(
            ValidationIssue(
                type="missing_column", row=None, column=col,
                message=f'Required column "{col}" is not in the file',
            )
        )
    if missing:
        # Without the required columns, per-row checks are meaningless.
        report = ValidationReport(
            valid=False, total_rows=0, valid_rows=0, duplicate_rows=0,
            errors=errors, warnings=warnings,
        )
        return report, []

    ref_values = await _load_ref_values(conn, table)
    col_by_name = {c.name: c for c in table.columns}

    rows = list(reader)
    total = len(rows)
    seen_rows: set[tuple] = set()
    period_seen: set[tuple] = set()
    period_values: list[dict] = []
    duplicate_count = 0

    for i, row in enumerate(rows, start=1):
        # type + null + fk checks per known column
        for col_name, raw in row.items():
            col = col_by_name.get(col_name)
            if col is None:
                continue  # extra columns are ignored, not an error
            ok, _ = _parse_type(raw, col.sql_type)
            if not ok:
                errors.append(ValidationIssue(
                    type="type_mismatch", row=i, column=col_name,
                    message=f'"{raw}" is not a valid {col.sql_type}',
                ))
                continue
            if (raw == "" or raw is None) and not col.nullable and col.default is None:
                errors.append(ValidationIssue(
                    type="unexpected_null", row=i, column=col_name,
                    message=f'"{col_name}" must not be empty',
                ))
            if col_name in ref_values and raw not in ("", None):
                if str(raw) not in ref_values[col_name]:
                    fk = table.fk_map[col_name]
                    errors.append(ValidationIssue(
                        type="unknown_code", row=i, column=col_name,
                        message=f'"{raw}" not found in {fk.ref_table} reference table',
                    ))

        # duplicate detection on the natural key (or whole row if none)
        key_cols = table.period_key or header
        key = tuple(row.get(c) for c in key_cols)
        if key in seen_rows:
            duplicate_count += 1
            warnings.append(ValidationIssue(
                type="duplicate_row", row=i, column=None,
                message="Duplicate of an earlier row in this file",
            ))
        else:
            seen_rows.add(key)

        # collect distinct period-key values
        if table.period_key:
            pkey = tuple(row.get(c) for c in table.period_key)
            if pkey not in period_seen:
                period_seen.add(pkey)
                period_values.append({c: row.get(c) for c in table.period_key})

    valid_rows = total - len({e.row for e in errors if e.row is not None})
    report = ValidationReport(
        valid=len(errors) == 0,
        total_rows=total,
        valid_rows=max(valid_rows, 0),
        duplicate_rows=duplicate_count,
        errors=errors,
        warnings=warnings,
    )
    return report, period_values


async def periods_present_in_live_table(
    conn: asyncpg.Connection, table: Table, period_values: list[dict]
) -> list[dict]:
    """Subset of period_values that already have rows in the live table."""
    if not table.period_key or not period_values:
        return []
    hits = []
    where = " AND ".join(f'"{c}" = ${i+1}' for i, c in enumerate(table.period_key))
    for pv in period_values:
        args = [pv.get(c) for c in table.period_key]
        # coerce ints where the column is integer, so "2024" matches 2024
        coerced = []
        for c, a in zip(table.period_key, args):
            col = table.column(c)
            if col and col.sql_type.startswith("int") and a not in (None, ""):
                try:
                    a = int(a)
                except ValueError:
                    pass
            coerced.append(a)
        exists = await conn.fetchval(
            f'SELECT EXISTS(SELECT 1 FROM "{table.name}" WHERE {where})', *coerced
        )
        if exists:
            hits.append(pv)
    return hits

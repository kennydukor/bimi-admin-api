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
    # Strip a stray BOM if utf-8-sig didn't catch it, and any leading blank lines.
    text = text.lstrip("\ufeff").lstrip("\r\n")

    # Sniff the delimiter — Excel/Numbers exports are often ';' or tab, not ','.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    raw_header = reader.fieldnames or []
    # Normalise header names: strip BOM, surrounding whitespace, and quotes.
    header = [h.lstrip("\ufeff").strip().strip('"').strip("'") for h in raw_header]
    # Rebuild the field map so downstream row dicts use the cleaned names.
    clean_map = dict(zip(raw_header, header))

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    table_col_names = {c.name for c in table.columns}

    # Guard: if NONE of the file's headers match any column in the table, the
    # file almost certainly wasn't parsed as we expect (wrong delimiter, an
    # extra title row above the header, or not a CSV). Say that plainly instead
    # of listing every required column as "missing".
    if not (set(header) & table_col_names):
        errors.append(
            ValidationIssue(
                type="missing_column", row=None, column=None,
                message=(
                    "Couldn't read the column header. Make sure the file is a "
                    "comma-separated CSV whose first row is the column names "
                    f"(detected delimiter '{delimiter}', first line: "
                    f"{(text.splitlines()[0][:80] if text.strip() else '<empty>')!r})."
                ),
            )
        )
        report = ValidationReport(
            valid=False, total_rows=0, valid_rows=0, duplicate_rows=0,
            errors=errors, warnings=warnings,
        )
        return report, []

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

    from app.services.fk_labels import resolver_for_table
    fk_resolvers = await resolver_for_table(conn, table)

    # Columns where a value that has never appeared before is *probably* a typo,
    # but might be legitimately new — so we warn, not error. Only for non-enum,
    # non-FK text columns that behave like a controlled vocabulary.
    VOCAB_COLUMNS = {"unit", "mda", "indicator_name", "project_status"}
    vocab_values: dict[str, set] = {}
    for c in table.columns:
        if c.name in VOCAB_COLUMNS and c.name not in fk_resolvers and not c.enum:
            rows_v = await conn.fetch(
                f'SELECT DISTINCT "{c.name}" AS v FROM "{table.name}" '
                f'WHERE "{c.name}" IS NOT NULL'
            )
            vocab_values[c.name] = {str(r["v"]).strip().lower() for r in rows_v}
    col_by_name = {c.name: c for c in table.columns}

    rows = [
        {clean_map.get(k, k): v for k, v in raw_row.items()}
        for raw_row in reader
    ]
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

            is_fk = col_name in fk_resolvers

            # FK columns: resolve name-or-code FIRST. The cell may hold a friendly
            # name (text) even though the column type is int, so we must not run
            # the raw type check on the unresolved value.
            if is_fk and raw not in ("", None):
                ok_fk, _code = fk_resolvers[col_name].resolve(raw)
                if not ok_fk:
                    fk = table.fk_map[col_name]
                    errors.append(ValidationIssue(
                        type="unknown_code", row=i, column=col_name,
                        message=f'"{raw}" is not a valid {fk.ref_table} code or name',
                    ))
                # resolved (or reported); skip the raw type check for this cell.
                if (raw == "" or raw is None) and not col.nullable and col.default is None:
                    errors.append(ValidationIssue(
                        type="unexpected_null", row=i, column=col_name,
                        message=f'"{col_name}" must not be empty',
                    ))
                continue

            # Enum columns (DB CHECK constraints): value must be one of the
            # allowed options. Hard error — Postgres would reject it anyway, but
            # we surface it in the report instead of failing at commit.
            if col.enum and raw not in ("", None):
                if str(raw).strip() not in col.enum:
                    errors.append(ValidationIssue(
                        type="invalid_enum", row=i, column=col_name,
                        message=(
                            f'"{raw}" is not allowed for "{col_name}". '
                            f'Expected one of: {", ".join(col.enum)}'
                        ),
                    ))
                continue

            # Vocabulary columns: warn (don't block) if the value has never been
            # seen in this column before — likely a typo, possibly legitimately new.
            if col_name in vocab_values and raw not in ("", None):
                if str(raw).strip().lower() not in vocab_values[col_name]:
                    warnings.append(ValidationIssue(
                        type="unseen_value", row=i, column=col_name,
                        message=(
                            f'"{raw}" has not appeared in "{col_name}" before — '
                            f"double-check it's not a typo."
                        ),
                    ))
                # still run the type check below for text this is a no-op

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

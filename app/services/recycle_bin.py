"""
Recycle bin — S3-backed soft delete for the production tables.

The design in one paragraph
---------------------------
The 35 production tables get no `deleted_at` column and no tombstone rows —
their shape stays exactly what the AI pipeline expects. To delete, we (1) SELECT
the target rows, (2) write them to a CSV in S3 with a JSON sidecar recording each
column's Postgres type, (3) record a *batch* row in `admin_recycle_batches` plus
per-row entries in `admin_recycle_rows`, and only then (4) DELETE the rows from
the production table — all inside one transaction, so a failure anywhere leaves
the data in place. Restore reverses it: read the CSV, re-INSERT with an explicit
column list (letting the sequence keep the original id via OVERRIDING is avoided
— we preserve the original pk), and mark the batch restored. A purge job deletes
the CSV and the batch once `purge_after` passes.

Two extra things this buys us, both surfaced in the UI:
  • a **recoverable count** for the dashboard (sum of active batch row counts);
  • a **"data for this period already exists"** flag — because each batch stores
    the period-key values of what it removed, we can tell an uploader that the
    period they're about to add currently sits in the recycle bin, so restoring
    is probably what they want instead of a fresh insert.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import asyncpg

from app.core.config import settings
from app.db import schema as schema_mod
from app.db.schema import Table, jsonable
from app.services.storage import get_storage


# ── CSV (de)serialisation ────────────────────────────────────
def _rows_to_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _csv_cell(row.get(c)) for c in columns})
    return buf.getvalue()


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""              # empty string ⇒ NULL on restore
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(jsonable(value))


def _csv_to_rows(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    return list(reader)


def _key(prefix: str, table_name: str, batch_id: str, ext: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    return f"{settings.s3_prefix}/{table_name}/{ts}/{batch_id}.{ext}"


# ── Type coercion on restore ─────────────────────────────────
def _coerce(value: str, sql_type: str) -> Any:
    if value == "":
        return None
    t = sql_type.lower()
    if t.startswith(("int", "serial")):
        return int(value)
    if t.startswith("numeric") or t in ("float4", "float8", "double precision", "real"):
        from decimal import Decimal
        return Decimal(value)
    if t.startswith("bool"):
        return value.lower() in ("t", "true", "1", "yes")
    if t.startswith("timestamp") or t.startswith("date"):
        return datetime.fromisoformat(value)
    return value


# ── Public operations ────────────────────────────────────────
class RecycleBin:
    """All soft-delete / restore logic. Constructed per request with a conn."""

    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn
        self._storage = get_storage()

    # --- delete ---------------------------------------------------------
    async def soft_delete(
        self,
        table: Table,
        *,
        where_sql: str,
        where_args: list[Any],
        scope: str,               # 'row' | 'bulk' | 'file'
        actor_id: uuid.UUID | None,
        actor_name: str,
        filter_summary: str | None = None,
        upload_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """
        Move every row matching `where_sql` to the recycle bin, then delete it
        from the production table. Runs in a transaction opened by the caller.
        Returns a summary dict {batch_id, deleted}.
        """
        columns = table.column_names
        select_cols = ", ".join(f'"{c}"' for c in columns)

        rows = await self._conn.fetch(
            f'SELECT {select_cols} FROM "{table.name}" WHERE {where_sql}',
            *where_args,
        )
        if not rows:
            return {"batch_id": None, "deleted": 0}

        dict_rows = [dict(r) for r in rows]
        batch_id = uuid.uuid4()
        pk = table.pk
        deleted_pks = [str(r[pk]) for r in dict_rows]
        period_values = self._period_values(table, dict_rows)

        # 1. Persist CSV + type sidecar to object storage.
        csv_text = _rows_to_csv(columns, dict_rows)
        meta = {
            "table": table.name,
            "pk": pk,
            "columns": [{"name": c.name, "sql_type": c.sql_type} for c in table.columns],
            "period_key": table.period_key,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
        csv_key = _key(settings.s3_prefix, table.name, str(batch_id), "csv")
        meta_key = _key(settings.s3_prefix, table.name, str(batch_id), "meta.json")
        self._storage.put_text(csv_key, csv_text, "text/csv")
        self._storage.put_text(meta_key, json.dumps(meta, indent=2), "application/json")

        # 2. Record the manifest.
        purge_after = datetime.now(timezone.utc) + timedelta(days=settings.recycle_retention_days)
        await self._conn.execute(
            """
            INSERT INTO admin_recycle_batches
                (id, table_name, scope, row_count, csv_key, meta_key,
                 deleted_pks, period_values, filter_summary,
                 deleted_by, deleted_by_name, purge_after)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            batch_id, table.name, scope, len(dict_rows), csv_key, meta_key,
            json.dumps(deleted_pks), json.dumps(period_values, default=str),
            filter_summary, actor_id, actor_name, purge_after,
        )
        await self._conn.executemany(
            """INSERT INTO admin_recycle_rows (batch_id, table_name, row_pk)
               VALUES ($1,$2,$3)""",
            [(batch_id, table.name, spk) for spk in deleted_pks],
        )

        # 3. Delete from the production table — same transaction.
        await self._conn.execute(
            f'DELETE FROM "{table.name}" WHERE {where_sql}', *where_args
        )

        # 4. If this was a whole-file delete, point the upload at the batch.
        if upload_id is not None:
            await self._conn.execute(
                "UPDATE admin_uploads SET deleted_at = now(), recycle_batch_id = $1 WHERE id = $2",
                batch_id, upload_id,
            )

        return {"batch_id": str(batch_id), "deleted": len(dict_rows)}

    # --- restore --------------------------------------------------------
    async def restore_batch(self, batch_id: uuid.UUID) -> dict[str, Any]:
        batch = await self._conn.fetchrow(
            "SELECT * FROM admin_recycle_batches WHERE id = $1 AND status = 'active'",
            batch_id,
        )
        if batch is None:
            raise LookupError("Recycle batch not found or already restored")

        table = schema_mod.registry.require(batch["table_name"])
        meta = json.loads(self._storage.get_text(batch["meta_key"]))
        type_by_col = {c["name"]: c["sql_type"] for c in meta["columns"]}
        csv_rows = _csv_to_rows(self._storage.get_text(batch["csv_key"]))

        restored = await self._insert_rows(table, csv_rows, type_by_col)

        await self._conn.execute(
            "UPDATE admin_recycle_batches SET status='restored', restored_at=now() WHERE id=$1",
            batch_id,
        )
        await self._conn.execute(
            "UPDATE admin_uploads SET deleted_at=NULL, recycle_batch_id=NULL WHERE recycle_batch_id=$1",
            batch_id,
        )
        return {"batch_id": str(batch_id), "restored": restored}

    async def restore_row(self, table: Table, row_pk: str) -> dict[str, Any]:
        """Restore a single row without restoring its whole batch."""
        rec = await self._conn.fetchrow(
            """SELECT batch_id FROM admin_recycle_rows
               WHERE table_name=$1 AND row_pk=$2 LIMIT 1""",
            table.name, str(row_pk),
        )
        if rec is None:
            raise LookupError("Row is not in the recycle bin")
        batch = await self._conn.fetchrow(
            "SELECT * FROM admin_recycle_batches WHERE id=$1", rec["batch_id"]
        )
        meta = json.loads(self._storage.get_text(batch["meta_key"]))
        type_by_col = {c["name"]: c["sql_type"] for c in meta["columns"]}
        csv_rows = _csv_to_rows(self._storage.get_text(batch["csv_key"]))
        target = [r for r in csv_rows if str(r.get(table.pk)) == str(row_pk)]
        if not target:
            raise LookupError("Row not found in stored CSV")

        await self._insert_rows(table, target, type_by_col)
        await self._conn.execute(
            "DELETE FROM admin_recycle_rows WHERE batch_id=$1 AND row_pk=$2",
            batch["id"], str(row_pk),
        )
        # If the batch is now empty, mark it restored.
        remaining = await self._conn.fetchval(
            "SELECT count(*) FROM admin_recycle_rows WHERE batch_id=$1", batch["id"]
        )
        if remaining == 0:
            await self._conn.execute(
                "UPDATE admin_recycle_batches SET status='restored', restored_at=now() WHERE id=$1",
                batch["id"],
            )
        return {"restored": 1}

    async def _insert_rows(
        self, table: Table, csv_rows: list[dict[str, str]], type_by_col: dict[str, str]
    ) -> int:
        if not csv_rows:
            return 0
        columns = [c for c in table.column_names if c in csv_rows[0]]
        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        # ON CONFLICT DO NOTHING so a partial re-upload of the same period does
        # not error — the natural-key unique index protects integrity.
        insert_sql = (
            f'INSERT INTO "{table.name}" ({col_list}) VALUES ({placeholders}) '
            f'ON CONFLICT DO NOTHING'
        )
        payload = [
            tuple(_coerce(row.get(c, ""), type_by_col.get(c, "text")) for c in columns)
            for row in csv_rows
        ]
        await self._conn.executemany(insert_sql, payload)
        return len(payload)

    # --- period-exists flag --------------------------------------------
    def _period_values(self, table: Table, rows: list[dict[str, Any]]) -> list[dict]:
        if not table.period_key:
            return []
        seen: set[tuple] = set()
        out: list[dict] = []
        for r in rows:
            key = tuple(r.get(c) for c in table.period_key)
            if key not in seen:
                seen.add(key)
                out.append({c: jsonable(r.get(c)) for c in table.period_key})
        return out

    async def periods_in_bin(
        self, table: Table, period_values: Iterable[dict]
    ) -> list[dict]:
        """
        Given period-key value dicts an uploader is about to insert, return the
        subset that currently sit in the recycle bin (active batches). Drives the
        "this period was recently deleted — restore instead?" warning.
        """
        if not table.period_key:
            return []
        batches = await self._conn.fetch(
            """SELECT period_values FROM admin_recycle_batches
               WHERE table_name=$1 AND status='active'""",
            table.name,
        )
        binned: set[tuple] = set()
        for b in batches:
            for pv in json.loads(b["period_values"]):
                binned.add(tuple(str(pv.get(c)) for c in table.period_key))

        hits = []
        for pv in period_values:
            key = tuple(str(pv.get(c)) for c in table.period_key)
            if key in binned:
                hits.append(pv)
        return hits

    # --- dashboard helpers ---------------------------------------------
    async def recoverable_row_count(self) -> int:
        val = await self._conn.fetchval(
            "SELECT COALESCE(SUM(row_count),0) FROM admin_recycle_batches WHERE status='active'"
        )
        return int(val or 0)

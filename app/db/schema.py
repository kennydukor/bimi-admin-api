"""
Schema registry — the backend's map of the 35 production tables.

Why this exists
---------------
Every generic endpoint (browse rows, edit a row, validate an upload, soft-delete
a batch) needs to know, for an arbitrary table: its primary key, its column types,
which columns are foreign keys, and — critically for the "same period already
exists" flag — which columns form its natural/business key.

Two ways to obtain that:

1. **Live introspection** (`load_from_db`): read `information_schema` and
   `pg_catalog` at startup. Authoritative; picks up migrations automatically.
2. **Static snapshot** (`load_from_snapshot`): a JSON extracted from the DDL,
   committed to the repo. Used as a fast default and an offline fallback.

At startup we try (1) and fall back to (2), then assert they agree so drift is
caught in CI rather than in production.

Nothing in here executes DML. It only *describes* tables so other modules can
build parameterised SQL safely — table and column names are always validated
against this registry before they reach a query, which is what keeps the
dynamic SQL injection-safe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from app.core.config import settings

_SNAPSHOT_PATH = Path(__file__).with_name("schema_snapshot.json")

# UI column type vocabulary, matching RowColumnType in the frontend's types/admin.ts.
UIType = str  # 'id' | 'select' | 'currency' | 'text' | 'number' | 'datetime'


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    nullable: bool
    default: str | None
    ui_type: UIType
    enum: list[str] | None = None

    @property
    def is_generated(self) -> bool:
        """Server-managed columns the admin never sets by hand."""
        return self.default in ("SEQUENCE", "now") or self.sql_type == "tsvector"


@dataclass(frozen=True)
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class Table:
    name: str
    pk: str
    columns: list[Column]
    fks: list[ForeignKey]
    is_reference: bool
    #: Columns that identify one logical observation. Used to detect that data
    #: for the same period/scope already exists. None for reference tables.
    period_key: list[str] | None
    category: str = "Uncategorised"
    frequency: str = "monthly"

    # ── convenience lookups ──────────────────────────────────
    _by_name: dict[str, Column] = field(default_factory=dict, compare=False)

    def column(self, name: str) -> Column | None:
        return self._by_name.get(name)

    def has_column(self, name: str) -> bool:
        return name in self._by_name

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def editable_columns(self) -> list[Column]:
        """Columns a Super Admin may set in a row edit — excludes pk and
        server-generated columns (sequences, created_at, search_vector)."""
        return [
            c
            for c in self.columns
            if c.name != self.pk and not c.is_generated and c.sql_type != "tsvector"
        ]

    @property
    def fk_map(self) -> dict[str, ForeignKey]:
        return {fk.column: fk for fk in self.fks}


class SchemaRegistry:
    """Holds every table's structure and answers questions about it."""

    def __init__(self, tables: dict[str, Table]):
        self._tables = tables

    # ── access ───────────────────────────────────────────────
    def all(self) -> list[Table]:
        return list(self._tables.values())

    def fact_tables(self) -> list[Table]:
        return [t for t in self._tables.values() if not t.is_reference]

    def reference_tables(self) -> list[Table]:
        return [t for t in self._tables.values() if t.is_reference]

    def get(self, name: str) -> Table | None:
        return self._tables.get(name)

    def require(self, name: str) -> Table:
        table = self._tables.get(name)
        if table is None:
            raise KeyError(f"Unknown table '{name}'")
        return table

    def is_known_table(self, name: str) -> bool:
        return name in self._tables

    def __len__(self) -> int:
        return len(self._tables)


# ── Human-friendly presentation metadata ─────────────────────
# Category groupings + labels for the UI. Structure (pk/fk/types) comes from the
# DB; this is the only presentation layer and is safe to hand-tune.
_CATEGORY: dict[str, str] = {
    # Federal & National
    "federal_faac_allocation": "Federal & National",
    "federal_macroeconomic_data": "Federal & National",
    "federal_contractor_payments": "Federal & National",
    "federal_budget_insertions": "Federal & National",
    "federal_budget_expenditure": "Federal & National",
    "federal_mda_project_expenditures": "Federal & National",
    "federal_sectoral_expenditure": "Federal & National",
    "federal_revenue": "Federal & National",
    "federal_capital_budget_utilization": "Federal & National",
    "federal_financing": "Federal & National",
    "federal_revenue_expenditure_budget_totals": "Federal & National",
    "national_labour_force": "Federal & National",
    "national_economic_data": "Federal & National",
    "national_debt": "Federal & National",
    "crude_oil_production": "Federal & National",
    "gas_production": "Federal & National",
    "cost_of_healthy_diet": "Federal & National",
    # State & Regional
    "state_fiscal_sustainability_ranking": "State & Regional",
    "state_women_affairs_budget": "State & Regional",
    "state_debt": "State & Regional",
    "state_labour_force": "State & Regional",
    "state_budget_expenditure": "State & Regional",
    "state_faac_allocation": "State & Regional",
    "states_internally_generated_revenue": "State & Regional",
    "state_total_revenue": "State & Regional",
    "state_sectoral_budget_expenditure": "State & Regional",
    "population": "State & Regional",
    "zonal_intervention_projects": "State & Regional",
    "lga_faac_allocation": "LGA",
    # Reference
    "currencies": "Reference Data",
    "lgas": "Reference Data",
    "sectors": "Reference Data",
    "states": "Reference Data",
    "sources": "Reference Data",
    "indicators": "Reference Data",
}

_LABEL_OVERRIDES: dict[str, str] = {
    "federal_faac_allocation": "Federal FAAC Allocation",
    "lga_faac_allocation": "LGA FAAC Allocation",
    "federal_mda_project_expenditures": "Federal MDA Project Expenditures",
    "states_internally_generated_revenue": "State Internally Generated Revenue",
    "lgas": "LGAs",
    "federal_revenue_expenditure_budget_totals": "Federal Revenue & Expenditure Totals",
}


def humanize(name: str) -> str:
    if name in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[name]
    return " ".join(w.capitalize() for w in name.split("_"))


def infer_frequency(table_meta: dict[str, Any]) -> str:
    """Best-guess reporting cadence from the period key, for the upload wizard."""
    pk = table_meta.get("period_key") or []
    if "month" in pk:
        return "monthly"
    if "quarter" in pk:
        return "quarterly"
    return "annual"


# ── Loading ──────────────────────────────────────────────────
def _build_table(name: str, meta: dict[str, Any]) -> Table:
    columns = [
        Column(
            name=c["name"],
            sql_type=c["sql_type"],
            nullable=c["nullable"],
            default=c.get("default"),
            ui_type=c.get("ui_type", "text"),
            enum=c.get("enum"),
        )
        for c in meta["columns"]
    ]
    fks = [
        ForeignKey(fk["column"], fk["ref_table"], fk["ref_column"])
        for fk in meta.get("fks", [])
    ]
    return Table(
        name=name,
        pk=meta["pk"],
        columns=columns,
        fks=fks,
        is_reference=meta.get("is_reference", False),
        period_key=meta.get("period_key"),
        category=_CATEGORY.get(name, "Uncategorised"),
        frequency=infer_frequency(meta),
        _by_name={c.name: c for c in columns},
    )


def load_from_snapshot() -> SchemaRegistry:
    raw = json.loads(_SNAPSHOT_PATH.read_text())
    tables = {name: _build_table(name, meta) for name, meta in raw.items()}
    return SchemaRegistry(tables)


async def load_from_db(pool: asyncpg.Pool) -> SchemaRegistry:
    """
    Introspect the live database. Reads columns, primary keys, foreign keys and
    unique constraints straight from the catalog so a migration is reflected
    without editing the snapshot.
    """
    schema = settings.db_schema

    col_rows = await pool.fetch(
        """
        SELECT table_name, column_name, data_type, udt_name,
               is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
        """,
        schema,
    )
    pk_rows = await pool.fetch(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = $1
        """,
        schema,
    )
    fk_rows = await pool.fetch(
        """
        SELECT tc.table_name, kcu.column_name,
               ccu.table_name AS ref_table, ccu.column_name AS ref_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = $1
        """,
        schema,
    )
    # Unique indexes → natural keys, in column order.
    uniq_rows = await pool.fetch(
        """
        SELECT t.relname AS table_name,
               a.attname AS column_name,
               array_position(ix.indkey, a.attnum) AS pos
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
        WHERE ix.indisunique AND NOT ix.indisprimary AND n.nspname = $1
        ORDER BY t.relname, pos
        """,
        schema,
    )

    # Assemble
    pk_by_table = {r["table_name"]: r["column_name"] for r in pk_rows}
    fks_by_table: dict[str, list[dict]] = {}
    for r in fk_rows:
        fks_by_table.setdefault(r["table_name"], []).append(
            {"column": r["column_name"], "ref_table": r["ref_table"], "ref_column": r["ref_column"]}
        )
    uniq_by_table: dict[str, list[str]] = {}
    for r in uniq_rows:
        uniq_by_table.setdefault(r["table_name"], []).append(r["column_name"])

    # Enum check constraints are harder to introspect portably; carry them over
    # from the snapshot which parsed them from the DDL.
    snap = json.loads(_SNAPSHOT_PATH.read_text())

    tables: dict[str, Table] = {}
    cols_by_table: dict[str, list[dict]] = {}
    for r in col_rows:
        cols_by_table.setdefault(r["table_name"], []).append(r)

    for tname, cols in cols_by_table.items():
        # The registry only manages the production tables described by the DDL
        # snapshot. Skip the portal's own admin_* bookkeeping tables and any
        # other table not in the snapshot, so they never appear in Datasets.
        if tname.startswith("admin_") or tname not in snap:
            continue
        snap_meta = snap.get(tname, {})
        snap_cols = {c["name"]: c for c in snap_meta.get("columns", [])}
        built_cols = []
        for c in cols:
            sc = snap_cols.get(c["column_name"], {})
            built_cols.append(
                {
                    "name": c["column_name"],
                    "sql_type": _pg_type(c),
                    "nullable": c["is_nullable"] == "YES",
                    "default": _default_kind(c["column_default"]),
                    "ui_type": sc.get("ui_type", "text"),
                    "enum": sc.get("enum"),
                }
            )
        nk = uniq_by_table.get(tname)
        if nk == [pk_by_table.get(tname)]:
            nk = None
        meta = {
            "columns": built_cols,
            "pk": pk_by_table.get(tname, "id"),
            "fks": fks_by_table.get(tname, []),
            "is_reference": snap_meta.get("is_reference", False),
            "period_key": snap_meta.get("period_key") or nk,
        }
        tables[tname] = _build_table(tname, meta)

    return SchemaRegistry(tables)


def _pg_type(row: asyncpg.Record) -> str:
    dt = row["data_type"]
    if dt == "USER-DEFINED":
        return row["udt_name"]
    return {"integer": "int4", "character varying": "varchar", "timestamp without time zone": "timestamp"}.get(dt, dt)


def _default_kind(default: str | None) -> str | None:
    if default is None:
        return None
    if "nextval" in default:
        return "SEQUENCE"
    if "now()" in default:
        return "now"
    return default.strip("'").split("::")[0]


# ── JSON coercion helper (asyncpg returns Decimal / datetime) ──
def jsonable(value: Any) -> Any:
    from datetime import date, datetime

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ", timespec="minutes") if isinstance(value, datetime) else value.isoformat()
    return value


# Module-level registry, populated at startup by app.main.lifespan.
registry: SchemaRegistry = load_from_snapshot()


def set_registry(new_registry: SchemaRegistry) -> None:
    global registry
    registry = new_registry

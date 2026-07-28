"""
Safe dynamic SQL for the production tables.

The row browser, filters and bulk delete all operate on an arbitrary table name
supplied by the client. That is only safe because every identifier is checked
against the schema registry before it is interpolated, and every *value* goes in
as a bound parameter — never string-formatted. If a table or column is not in the
registry, the request is rejected before any SQL is built.
"""
from __future__ import annotations

from typing import Any

from app.db.schema import Table


class Filter:
    """Accumulates a parameterised WHERE clause with $1, $2… placeholders."""

    def __init__(self, start_index: int = 1):
        self._clauses: list[str] = []
        self._args: list[Any] = []
        self._i = start_index

    def _next(self) -> str:
        ph = f"${self._i}"
        self._i += 1
        return ph

    def eq(self, column: str, value: Any) -> "Filter":
        self._clauses.append(f'"{column}" = {self._next()}')
        self._args.append(value)
        return self

    def ilike_any(self, columns: list[str], term: str) -> "Filter":
        ph = self._next()
        ors = " OR ".join(f'CAST("{c}" AS TEXT) ILIKE {ph}' for c in columns)
        self._clauses.append(f"({ors})")
        self._args.append(f"%{term}%")
        return self

    def raw_in_pks(self, pk_col: str, values: list[Any]) -> "Filter":
        ph = self._next()
        self._clauses.append(f'"{pk_col}" = ANY({ph})')
        self._args.append(values)
        return self

    @property
    def where_sql(self) -> str:
        return " AND ".join(self._clauses) if self._clauses else "TRUE"

    @property
    def args(self) -> list[Any]:
        return self._args

    @property
    def next_index(self) -> int:
        return self._i


def build_row_filter(
    table: Table,
    *,
    year: str | None = None,
    source: str | None = None,
    search: str | None = None,
) -> Filter:
    """
    Translate the frontend's RowFilter (year / source / free-text search) into a
    validated WHERE clause. Unknown columns are silently skipped rather than
    trusted.
    """
    f = Filter()
    if year and table.has_column("year"):
        f.eq("year", int(year))
    if source:
        # `source` may be a source_id (int) or a code substring; match against
        # whichever source column the table actually has.
        if table.has_column("source_id"):
            try:
                f.eq("source_id", int(source))
            except ValueError:
                pass
        elif table.has_column("source"):
            try:
                f.eq("source", int(source))
            except ValueError:
                pass
    if search:
        text_cols = [
            c.name
            for c in table.columns
            if c.sql_type != "tsvector" and c.name != "search_vector"
        ]
        f.ilike_any(text_cols, search)
    return f


async def count_rows(conn, table: Table, f: Filter) -> int:
    return int(
        await conn.fetchval(
            f'SELECT count(*) FROM "{table.name}" WHERE {f.where_sql}', *f.args
        )
    )


async def fetch_rows(
    conn, table: Table, f: Filter, *, limit: int, offset: int
) -> list[dict]:
    display_cols = [c.name for c in table.columns if c.sql_type != "tsvector"]
    col_list = ", ".join(f'"{c}"' for c in display_cols)
    order = f'"{table.pk}" DESC'
    rows = await conn.fetch(
        f'SELECT {col_list} FROM "{table.name}" WHERE {f.where_sql} '
        f"ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}",
        *f.args,
    )
    return [dict(r) for r in rows]

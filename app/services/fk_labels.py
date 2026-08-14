"""
Foreign-key label lookups.

FK columns store a code (state_code='LA', source_id=2). For display we want the
friendly name ('Lagos', 'National Bureau of Statistics'). This module loads, per
reference table, a {code: name} map so the API can:

  • attach human labels to rows for the table view, and
  • offer name-labelled options (value=code) for the edit dropdown,

without ever changing the stored value — the code remains authoritative for
uploads, edits, and FK integrity.

Maps are cached process-wide and refreshed lazily; reference tables change
rarely, and any admin edit to them can call `invalidate()`.
"""
from __future__ import annotations

import asyncpg

from app.db.schema import Table, registry

# Which column on each reference table is the friendly name.
_NAME_COLUMN = {
    "states": "state_name",
    "sectors": "sector_name",
    "currencies": "currency_name",
    "indicators": "indicator_name",
    "lgas": "lga_name",
    "sources": "source",          # sources has no *_name column; 'source' is it
}

_cache: dict[str, dict[str, str]] = {}


def invalidate(ref_table: str | None = None) -> None:
    if ref_table is None:
        _cache.clear()
    else:
        _cache.pop(ref_table, None)


async def _load(conn: asyncpg.Connection, ref_table: str, key_col: str) -> dict[str, str]:
    if ref_table in _cache:
        return _cache[ref_table]
    name_col = _NAME_COLUMN.get(ref_table)
    if name_col is None:
        _cache[ref_table] = {}
        return {}
    rows = await conn.fetch(f'SELECT "{key_col}" AS k, "{name_col}" AS v FROM "{ref_table}"')
    mapping = {str(r["k"]): (r["v"] if r["v"] is not None else str(r["k"])) for r in rows}
    _cache[ref_table] = mapping
    return mapping


async def labels_for_table(conn: asyncpg.Connection, table: Table) -> dict[str, dict[str, str]]:
    """
    For each FK column on `table`, return {column_name: {code: friendly_name}}.
    Empty dict if the table has no FKs.
    """
    out: dict[str, dict[str, str]] = {}
    for fk in table.fks:
        out[fk.column] = await _load(conn, fk.ref_table, fk.ref_column)
    return out


async def fk_options_for_column(
    conn: asyncpg.Connection, table: Table, column_name: str
) -> list[dict[str, str]] | None:
    """
    Options for an FK column's edit dropdown: [{value: code, label: name}, …],
    sorted by label. None if the column isn't an FK.
    """
    fk = table.fk_map.get(column_name)
    if fk is None:
        return None
    mapping = await _load(conn, fk.ref_table, fk.ref_column)
    opts = [{"value": code, "label": name} for code, name in mapping.items()]
    opts.sort(key=lambda o: o["label"].lower())
    return opts

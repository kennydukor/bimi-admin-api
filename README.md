# Bimi Admin API

FastAPI backend for the Bimi Phase 3 admin portal. It powers the Next.js
frontend in `bimi_admin/` — every `/api/v1/admin/*` endpoint the frontend's
`server/*.ts` clients call is implemented here, with request/response shapes that
match `types/admin.ts` exactly. Flip `NEXT_PUBLIC_USE_MOCKS=false` in the
frontend and it talks to this API with no other changes.

## What it does, at a glance

- **Auth** — email + password, httpOnly cookie sessions, email verification and
  password reset. Role gating (`regular_admin` / `super_admin`) as dependencies.
- **Datasets & rows** — browse, filter, edit, and delete rows across all 35
  production tables, driven entirely by live schema introspection.
- **Uploads** — CSV validated against the real table (columns, types, FK codes,
  duplicates, nulls) before a single row is committed.
- **Users, audit log, dashboard** — scoped server-side by role.
- **S3-backed soft delete** — see below.

## The database, untouched

The 35 tables the AI assistant queries are **never altered** by the portal — no
`deleted_at` columns, no tombstone rows, no triggers. They keep exactly the shape
the query pipeline expects. Everything the portal needs lives in additive
`admin_*` tables (`migrations/001_admin_tables.sql`):

```
admin_users            admin_sessions       admin_auth_tokens
admin_uploads          admin_audit_log
admin_recycle_batches  admin_recycle_rows
```

Table structure (primary keys, foreign keys, column types, enum constraints, and
the natural/business keys) is read from the live catalog at startup by
`app/db/schema.py`, with a committed snapshot (`schema_snapshot.json`, extracted
from your DDL) as an offline fallback. Add a table via migration and it appears
in the portal automatically — no code change.

## Soft delete: the S3 recycle bin

Deletes don't mutate the production tables in place. Each delete is a small
transaction:

1. **SELECT** the target rows.
2. **Export** them to `s3://<bucket>/deletions/<table>/<date>/<batch>.csv`, with a
   `.meta.json` sidecar recording each column's Postgres type (so a restore
   re-types values correctly, even years later).
3. **Record** a batch in `admin_recycle_batches` + per-row keys in
   `admin_recycle_rows`, including the *period-key values* of what was removed.
4. **DELETE** the rows from the production table.

If anything fails, the transaction rolls back and the data stays put — the CSV is
written before the DELETE, never after. **Restore** reads the CSV and re-inserts
with `ON CONFLICT DO NOTHING` (the table's own unique index is the backstop).
A purge job removes CSVs whose `purge_after` (deletion + 30 days) has passed.

Two things this design surfaces in the UI:

- **Recoverable rows** on the dashboard — `SUM(row_count)` over active batches.
- **"Data for this period already exists"** — because each batch stores the
  period-key values it removed, an upload targeting a period that is currently
  live *or sitting in the bin* comes back with a warning, so the uploader can
  restore instead of double-inserting. The period key per table is derived from
  the DDL's unique indexes (e.g. FAAC = `(year, month)`, state budget =
  `(state_code, year, month, quarter, budget_type)`).

In dev, leave the AWS variables blank and a local-filesystem backend
(`_recycle_bin_local/`) stands in for S3 — the portal runs end to end with no
cloud account.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # point DATABASE_URL at budgit_ai

python -m scripts.init_db       # create admin_* tables + seed demo users
uvicorn app.main:app --reload   # http://localhost:8000  (docs at /docs)
```

Demo Super Admin after seeding: `demlabz@gmail.com` / `changeme123`.

## Layout

```
app/
  main.py                 app factory; startup introspection + pool
  core/       config.py   settings (DB, S3, auth)
              security.py  password hashing, sessions, role deps
  db/         schema.py    schema registry (introspection + snapshot)
              session.py   asyncpg pool
              schema_snapshot.json
  services/   recycle_bin.py   S3 soft delete / restore / period checks
              storage.py       S3 + local backends
              validation.py    CSV validation against real tables
              query_builder.py safe parameterised filters
              audit.py         audit logging
  schemas/    admin.py     response models mirroring types/admin.ts
  api/v1/admin/            auth, dashboard, tables, datasets, uploads,
                           users, audit, recycle
migrations/   001_admin_tables.sql
scripts/      init_db.py
```

## Note on security

Every table and column name that reaches SQL is validated against the schema
registry first; every value is a bound parameter. An unknown table or column is
rejected before any query is built, which is what makes the generic row/upload
endpoints injection-safe.
# bimi-admin-api

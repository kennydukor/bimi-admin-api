# Bimi Admin API — Setup Guide

This walks you from a fresh checkout to a running backend the Next.js admin
frontend can talk to. Takes about 15 minutes on a machine that already has
Postgres and Python.

- [1. What you need](#1-what-you-need)
- [2. Get the code and install](#2-get-the-code-and-install)
- [3. Point it at the database](#3-point-it-at-the-database)
- [4. Choose a recycle-bin backend (S3 or local)](#4-choose-a-recycle-bin-backend)
- [5. Create the admin tables and seed a user](#5-create-the-admin-tables-and-seed-a-user)
- [6. Run the server](#6-run-the-server)
- [7. Connect the frontend](#7-connect-the-frontend)
- [8. Try it in Postman](#8-try-it-in-postman)
- [9. Verify end to end](#9-verify-end-to-end)
- [Troubleshooting](#troubleshooting)
- [Going to production](#going-to-production)

---

## 1. What you need

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | `python3 --version` |
| PostgreSQL | 13+ | The existing `budgit_ai` database (the one the assistant reads). |
| Access to that DB | — | A role that can `CREATE TABLE` in the `public` schema (only `admin_*` tables are created). |
| AWS S3 *(optional)* | — | For the recycle bin in production. In dev you can skip it — see step 4. |

The API **only adds** `admin_*` tables. It never alters the 35 production
tables, so pointing it at your real `budgit_ai` is safe.

---

## 2. Get the code and install

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Point it at the database

Copy the example env file and edit `DATABASE_URL`:

```bash
cp .env.example .env
```

```dotenv
# .env
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/budgit_ai
SESSION_SECRET=<paste a long random string>
```

Generate a session secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

> The API reads structure (tables, columns, keys) from this database at startup.
> If it can't connect at boot it still starts, falling back to the committed
> `schema_snapshot.json` — but you'll want the real connection for anything to
> actually read or write.

---

## 4. Choose a recycle-bin backend

Deleted rows are archived as CSV before they leave Postgres. That archive lives
in S3 — or, for local development, on disk.

**Local (no AWS needed).** Leave the AWS lines in `.env` commented out. The API
writes archives to `./_recycle_bin_local/`. Good enough to develop and demo the
full delete → restore flow.

**S3 (production).** Create a private bucket, then set:

```dotenv
S3_BUCKET=bimi-admin-recycle-bin
S3_REGION=eu-west-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

The IAM identity needs `s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject` on
`arn:aws:s3:::bimi-admin-recycle-bin/*`. Nothing needs to be public — downloads
use short-lived presigned URLs.

**S3-compatible (MinIO / LocalStack).** Also set `S3_ENDPOINT_URL`, e.g.
`http://localhost:9000`.

---

## 5. Create the admin tables and seed a user

```bash
python -m scripts.init_db
```

This runs `migrations/001_admin_tables.sql` (idempotent — safe to re-run) and
seeds demo users. The first Super Admin:

```
email:    demlabz@gmail.com
password: changeme123
```

Change that password immediately in any shared environment. To migrate without
seeding demo users (e.g. staging), run `python -m scripts.init_db --migrate` and
create your own first user by inserting into `admin_users` with a bcrypt hash.

---

## 6. Run the server

```bash
uvicorn app.main:app --reload
```

- API root: `http://localhost:8000`
- Interactive docs (Swagger): `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health` → `{"status":"ok","tables":35}`

If `tables` reads 35, schema introspection connected to your database correctly.

---

## 7. Connect the frontend

The Next.js app ships with a mock layer on. Turn it off and point it here. In the
frontend project (`bimi_admin/`), create `.env.local`:

```dotenv
NEXT_PUBLIC_USE_MOCKS=false
NEXT_PUBLIC_BASE_URL=http://localhost:8000
```

Then `npm run dev`. That's the only change — the frontend's API client
(`server/http.ts`) already switches from mocks to real `fetch` on this flag, and
every endpoint shape matches what it expects.

**Cookies across ports.** The session is an httpOnly cookie. In development the
frontend (`:3000`) and API (`:8000`) are different origins, so:

- the API already sends CORS with credentials for `http://localhost:3000`
  (`CORS_ORIGINS` in `.env`); add other origins there if needed;
- the cookie is set `SameSite=Lax`, `Secure=false` in development. In production,
  serve both behind the same domain (or set up HTTPS) so the cookie is accepted.

---

## 8. Try it in Postman

Import `bimi-admin-api.postman_collection.json`.

1. Set the collection variable `base_url` if it isn't `http://localhost:8000`.
2. Run **Auth → Login** first. It sets the session cookie in Postman's cookie
   jar, which is then sent automatically on every other request.
3. Explore: **Tables & Rows → List tables**, **Dashboard → Get stats**, etc.

Fill in the `table_name`, `row_id`, `dataset_id`, `user_id`, and `batch_id`
collection variables as you go (copy ids from list responses). Requests labelled
*(Super Admin)* need a Super Admin session; log in as `demlabz@gmail.com` for
those.

---

## 9. Verify end to end

A quick pass that exercises the soft-delete design:

1. **Login** as the Super Admin.
2. **List tables** → confirm 35 come back.
3. **Browse rows** of `federal_faac_allocation`.
4. **Soft-delete a row** (note its `id`).
5. **Browse deleted rows** (`?deleted=true`) → the row appears with who deleted it
   and when.
6. Check `./_recycle_bin_local/deletions/...` (local) or the S3 bucket → a CSV and
   a `.meta.json` are there.
7. **Restore the row** → it's gone from the deleted list and back in the table.
8. **Dashboard** → `recoverable_rows` reflects what's currently in the bin.

If all eight behave, the archive-before-delete path and restore are wired
correctly.

---

## Troubleshooting

**`/health` shows `"tables": 0` or a startup warning about introspection.**
The API couldn't read the database and fell back to the snapshot for structure
but has no live connection for data. Check `DATABASE_URL` and that the role can
read `information_schema`.

**`401 Not authenticated` on every call.**
You haven't logged in, or the cookie isn't being sent. In Postman, run Login
first. In the browser, confirm the frontend's `NEXT_PUBLIC_BASE_URL` matches the
API origin and that CORS allows your frontend origin with credentials.

**`403 Super Admin access required.`**
You're logged in as a Regular Admin. Users, dataset restore, row edit/delete, and
recycle-bin management require Super Admin.

**Upload rejected with a validation report.**
That's the point — the report lists exactly which rows/columns failed (missing
columns, type mismatches, unknown reference codes, nulls). Fix the CSV and
re-upload. A warning that "data for this period already exists" is informational;
the commit still proceeds unless there are hard errors.

**`boto3` errors on delete in production.**
Check the bucket name/region and that the IAM identity has the three S3 actions
listed in step 4.

---

## Going to production

- [ ] Set `ENVIRONMENT=production` (makes the session cookie `Secure`).
- [ ] Real `SESSION_SECRET`, rotated out of the example value.
- [ ] Serve frontend and API on the same domain, or configure HTTPS + CORS so the
      session cookie is accepted cross-origin.
- [ ] Real S3 bucket with least-privilege IAM; keep it private.
- [ ] Wire an email provider for invites and password resets — the tokens are
      already generated and stored; search the codebase for `TODO` where the send
      call goes.
- [ ] Schedule the recycle-bin purge (delete archives past `purge_after`,
      default 30 days) as a cron / scheduled task.
- [ ] Put the API behind your normal reverse proxy / TLS.
```

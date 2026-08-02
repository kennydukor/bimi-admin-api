# Bimi Admin API — Deployment (Docker + GitHub Actions)

Two ways to run the container: locally with the full self-contained stack, and
in production via the CI/CD pipeline that builds an image and rolls it out to
your server.

- [Run locally with Docker](#run-locally-with-docker)
- [How the pipeline works](#how-the-pipeline-works)
- [One-time setup for deploys](#one-time-setup-for-deploys)
- [The deploy target (server prerequisites)](#the-deploy-target)
- [Deploying](#deploying)
- [Other hosts (ECS, Fly, Render, Railway)](#other-hosts)
- [Operations](#operations)

---

## Run locally with Docker

`docker-compose.yml` (repo root) brings up the API, a throwaway Postgres, and
MinIO (S3-compatible) — the whole portal, recycle bin included, with no cloud
account:

```bash
# optional: let the DB start with your 35 tables so there's data to introspect
mkdir -p backend/seed && cp /path/to/alldatastructure.sql backend/seed/

docker compose up --build
```

- API → http://localhost:8000  (docs at `/health`, `/docs`)
- MinIO console → http://localhost:9001  (minioadmin / minioadmin)
- Migrations run automatically on boot (`RUN_MIGRATIONS=1`), seeding the demo
  Super Admin `demlabz@gmail.com` / `changeme123`.

Point the frontend at it with `NEXT_PUBLIC_USE_MOCKS=false` and
`NEXT_PUBLIC_BASE_URL=http://localhost:8000`.

To build just the image by hand:

```bash
docker build -t bimi-admin-api ./backend
docker run --rm -p 8000:8000 --env-file backend/.env bimi-admin-api
```

---

## How the pipeline works

`.github/workflows/deploy.yml` runs on every push to `main` (and on `v*` tags):

1. **check** — compiles every module; fails fast on broken code.
2. **build** — builds `backend/Dockerfile` and pushes to the GitHub Container
   Registry (`ghcr.io/<owner>/<repo>`), tagged with the commit SHA and `latest`.
   This needs **no secrets** — the built-in `GITHUB_TOKEN` can push to GHCR.
3. **deploy** — SSHes to your server, runs migrations once as a throwaway
   container, then rolls the service to the new image. **Skipped automatically**
   if you haven't set a deploy host, so build/push works immediately and you can
   wire up deployment when ready.

So out of the box, merging to `main` gives you a versioned image in GHCR. Add the
secrets below to also auto-deploy.

---

## One-time setup for deploys

Add these as repository secrets (**Settings → Secrets and variables → Actions**):

| Secret | What it is |
|---|---|
| `SSH_HOST` | Server hostname/IP. **Its presence is what turns deployment on.** |
| `SSH_USER` | SSH user (must be in the `docker` group). |
| `SSH_KEY` | Private key for that user (the matching public key is in the server's `authorized_keys`). |
| `DATABASE_URL` | `postgresql://…/budgit_ai` |
| `SESSION_SECRET` | Long random string. |
| `CORS_ORIGINS` | e.g. `["https://admin.bimi.example"]` |
| `S3_BUCKET`, `S3_REGION` | Recycle-bin bucket. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | IAM creds with `PutObject`/`GetObject`/`DeleteObject` on that bucket. |

Also create a GitHub **Environment** named `production` (Settings → Environments)
if you want a manual approval gate before deploys — the workflow already targets
it.

---

## The deploy target

The server just needs Docker and a directory the workflow can write to:

```bash
# on the server, once
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"          # log out/in after this
sudo mkdir -p /opt/bimi-admin && sudo chown "$USER" /opt/bimi-admin
```

The workflow copies `docker-compose.prod.yml` there, writes `.env` from your
secrets, logs Docker in to GHCR, pulls the image, migrates, and starts the
service. Nothing sensitive is stored on the server outside that `.env` (mode
`600`).

Put a reverse proxy (Caddy, Nginx, Traefik) in front of port 8000 for TLS and
your domain. Serving the frontend and API under the same domain avoids
cross-origin cookie friction.

---

## Deploying

Just push to `main`:

```bash
git push origin main
```

Watch it in the repo's **Actions** tab. Or trigger manually with
**Run workflow** (the `workflow_dispatch` trigger). Tagging a release
(`git tag v1.0.0 && git push --tags`) also builds an image tagged `v1.0.0`.

Roll back by re-running the workflow on an earlier commit, or on the server:

```bash
cd /opt/bimi-admin
IMAGE=ghcr.io/OWNER/REPO:sha-<old-sha> docker compose -f docker-compose.prod.yml up -d
```

---

## Other hosts

The image is a standard OCI container, so anything that runs one works — only the
`deploy` job changes:

- **AWS ECS / Fargate** — push the same image (or mirror to ECR), point a task
  definition at it, inject the env vars as task secrets. Run migrations as a
  one-off task.
- **Fly.io** — `fly launch` with a `fly.toml`; set secrets via `fly secrets set`.
- **Render / Railway** — connect the repo, choose "Docker", set the env vars in
  their dashboard. Both build the Dockerfile for you, so you can even drop the
  build job.

In every case: it's a stateless HTTP container on port 8000 that needs the same
env vars, plus a one-off `python -m scripts.init_db --migrate` against the DB.

---

## Operations

**Migrations.** Idempotent (`CREATE TABLE IF NOT EXISTS`). The deploy runs them
as a separate step so multi-worker/replica rollouts don't race. To run by hand:

```bash
docker compose -f docker-compose.prod.yml run --rm \
  -e RUN_MIGRATIONS=1 api python -m scripts.init_db --migrate
```

**Logs.** `docker compose -f docker-compose.prod.yml logs -f api`

**Health.** The container has a `HEALTHCHECK` hitting `/health`; `docker ps`
shows `healthy`. Your proxy/orchestrator can use the same endpoint.

**Recycle-bin purge.** Still to be scheduled — add a cron on the host (or a
scheduled task in your orchestrator) that removes S3 archives past
`purge_after`. The retention window is `RECYCLE_RETENTION_DAYS` (default 30).

**Scaling.** `WEB_CONCURRENCY` sets gunicorn workers per container. To run
several containers, put them behind the proxy and set `RUN_MIGRATIONS=0`
everywhere (migrations already run as their own step).

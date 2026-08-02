#!/bin/sh
# ============================================================
# Container entrypoint.
#
# If RUN_MIGRATIONS=1, apply the admin-table migration (idempotent) before
# starting the server. Handy for single-instance deploys. For multi-replica
# rollouts, leave it 0 and run migrations once as a separate job/step instead,
# so replicas don't race.
# ============================================================
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] applying migrations…"
  # --migrate: schema only, no demo-user seeding in real environments.
  python -m scripts.init_db --migrate
  echo "[entrypoint] migrations done."
fi

# Hand off to the CMD (gunicorn). exec so signals reach the server for graceful
# shutdown.
exec "$@"

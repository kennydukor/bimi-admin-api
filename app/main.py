"""
FastAPI application entrypoint.

On startup: open the asyncpg pool, then introspect the live schema and swap the
registry in (falling back to the committed snapshot if the DB is unreachable, so
the process still boots in dev). On shutdown: close the pool.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.admin import router as admin_router
from app.core.config import settings
from app.db import schema as schema_mod
from app.db.session import close_pool, open_pool

log = logging.getLogger("bimi.admin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await open_pool()
        try:
            live = await schema_mod.load_from_db(pool)
            schema_mod.set_registry(live)
            log.info("Schema introspected from database: %d tables", len(live))
        except Exception as exc:  # noqa: BLE001
            log.warning("DB introspection failed (%s); using committed snapshot", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Database pool unavailable at startup (%s); snapshot only", exc)
    yield
    await close_pool()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,   # cookies must be allowed to cross origins
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok", "tables": len(schema_mod.registry)}

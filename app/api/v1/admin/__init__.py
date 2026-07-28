"""Assembles every admin sub-router under a single prefix."""
from fastapi import APIRouter

from app.api.v1.admin import (
    audit,
    auth,
    dashboard,
    datasets,
    recycle,
    tables,
    uploads,
    users,
)

router = APIRouter(prefix="/api/v1/admin")
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(tables.router)     # /tables and /tables/{t}/rows/*
router.include_router(datasets.router)
router.include_router(uploads.router)
router.include_router(users.router)
router.include_router(audit.router)
router.include_router(recycle.router)

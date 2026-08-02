"""
Apply the admin-table migration and seed a first Super Admin + sample data.

    python -m scripts.init_db            # migrate + seed demo users
    python -m scripts.init_db --migrate  # migrate only

Idempotent: safe to re-run. Never touches the 35 production tables.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

from app.core.config import settings
from app.core.security import hash_password

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_admin_tables.sql"


async def migrate(conn: asyncpg.Connection) -> None:
    await conn.execute(MIGRATION.read_text())
    print("✓ admin tables migrated")


async def seed(conn: asyncpg.Connection) -> None:
    existing = await conn.fetchval("SELECT count(*) FROM admin_users")
    if existing:
        print(f"• users already present ({existing}); skipping seed")
        return

    demo = [
        ("demlabz@gmail.com", "John Doe", "super_admin", "active", "changeme123"),
        ("kennydukor@gmail.com", "Kenechi Dukor", "super_admin", "active", "changeme123"),
    ]
    for email, name, role, status, pw in demo:
        await conn.execute(
            """INSERT INTO admin_users (email, full_name, role, status, password_hash)
               VALUES ($1,$2,$3,$4,$5)""",
            email, name, role, status, hash_password(pw) if pw else None,
        )
    print(f"✓ seeded {len(demo)} demo users (password: changeme123)")


async def main() -> None:
    migrate_only = "--migrate" in sys.argv
    conn = await asyncpg.connect(settings.database_url)
    try:
        await migrate(conn)
        if not migrate_only:
            await seed(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

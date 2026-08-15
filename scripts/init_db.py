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

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


async def migrate(conn: asyncpg.Connection) -> None:
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.execute(path.read_text())
        print(f"✓ applied {path.name}")


async def seed(conn: asyncpg.Connection) -> None:
    existing = await conn.fetchval("SELECT count(*) FROM admin_users")
    if existing:
        print(f"• users already present ({existing}); skipping seed")
        return

    demo = [
        ("demlabz@gmail.com", "John Doe", "super_admin", "active", "changeme123"),
        ("ngozi.okonkwo@bimi.gov.ng", "Ngozi Okonkwo", "super_admin", "active", "changeme123"),
        ("ibrahim.musa@bimi.gov.ng", "Ibrahim Musa", "regular_admin", "active", "changeme123"),
        ("funke.adeyemi@bimi.gov.ng", "Funke Adeyemi", "regular_admin", "suspended", "changeme123"),
        ("amina.suleiman@bimi.gov.ng", "Amina Suleiman", "regular_admin", "pending_verification", None),
    ]
    for email, name, role, status, pw in demo:
        await conn.execute(
            """INSERT INTO admin_users (email, full_name, role, status, password_hash, must_change_password)
               VALUES ($1,$2,$3,$4,$5,false)""",
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

"""Apply db/schema.sql to the database in DATABASE_URL (TRD §5).

    python scripts/apply_schema.py

Idempotent-ish: if the tables already exist it says so rather than erroring out.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv(".env")


async def _connect(dsn: str) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(dsn)
    except Exception:
        # Managed Postgres (Neon/Supabase) requires SSL; retry explicitly if the
        # DSN's sslmode wasn't honored by the driver.
        return await asyncpg.connect(dsn, ssl=True)


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set in .env")
        return 1

    sql = pathlib.Path("db/schema.sql").read_text()
    con = await _connect(dsn)
    try:
        try:
            await con.execute(sql)
            print("Schema applied.")
        except asyncpg.exceptions.DuplicateTableError:
            print("Tables already exist — schema was already applied.")
        rows = await con.fetch(
            "select tablename from pg_tables where schemaname = 'public' order by tablename"
        )
        print("Tables in database:", ", ".join(r["tablename"] for r in rows) or "(none)")
    finally:
        await con.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

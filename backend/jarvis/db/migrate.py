"""Reproducible SQL migration runner: applies database/migrations/*.sql in order, once each,
inside a transaction, recording a checksum. Refuses to run if an applied file has changed."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "database" / "migrations"


async def migrate(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    conn = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await conn.execute("""create table if not exists schema_migrations (
            filename text primary key, sha256 text not null, applied_at timestamptz not null default now())""")
        await conn.execute("select pg_advisory_lock(727171)")
        done = {r["filename"]: r["sha256"] for r in await conn.fetch("select filename, sha256 from schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text()
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise RuntimeError(f"applied migration {path.name} was modified; create a new migration instead")
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("insert into schema_migrations(filename, sha256) values ($1, $2)", path.name, digest)
            applied_now.append(path.name)
        return applied_now
    finally:
        await conn.execute("select pg_advisory_unlock(727171)")
        await conn.close()


def main() -> None:
    from jarvis.config import get_settings

    applied = asyncio.run(migrate(get_settings().database_url))
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    sys.exit(0)


if __name__ == "__main__":
    main()

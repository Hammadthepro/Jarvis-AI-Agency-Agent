"""Reproducible SQL migration runner: applies database/migrations/*.sql in order, once each, inside a transaction,
recording a checksum in schema_migrations. Refuses to run if an applied file has changed. Safe to run repeatedly
(an advisory lock serialises concurrent runners; applied files are skipped).

    python -m jarvis.db.migrate            apply pending migrations (exit 1 with the error if one fails)
    python -m jarvis.db.migrate status     list applied / pending / modified (exit 3 if anything is pending)
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "database" / "migrations"


def _files(directory: Path) -> list[tuple[str, str, str]]:
    out = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text()
        out.append((path.name, hashlib.sha256(sql.encode()).hexdigest(), sql))
    return out


@dataclass
class MigrationStatus:
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.pending and not self.modified

    def describe(self) -> str:
        if self.ok:
            return f"up to date ({len(self.applied)} applied)"
        parts = []
        if self.pending:
            parts.append("pending: " + ", ".join(self.pending))
        if self.modified:
            parts.append("modified after being applied: " + ", ".join(self.modified))
        return "; ".join(parts) + ". Run: python -m jarvis.db.migrate (or scripts/dev-up.sh)"


async def status_conn(conn, directory: Path = MIGRATIONS_DIR) -> MigrationStatus:
    exists = await conn.fetchval("select to_regclass('public.schema_migrations') is not null")
    done = {r["filename"]: r["sha256"] for r in await conn.fetch("select filename, sha256 from schema_migrations")} if exists else {}
    st = MigrationStatus()
    for name, digest, _ in _files(directory):
        if name not in done:
            st.pending.append(name)
        elif done[name] != digest:
            st.modified.append(name)
        else:
            st.applied.append(name)
    return st


async def status(dsn: str, directory: Path = MIGRATIONS_DIR) -> MigrationStatus:
    from jarvis.config import normalize_dsn
    conn = await asyncpg.connect(normalize_dsn(dsn), timeout=10)
    try:
        return await status_conn(conn, directory)
    finally:
        await conn.close()


async def migrate(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    from jarvis.config import normalize_dsn
    conn = await asyncpg.connect(normalize_dsn(dsn), timeout=10)
    applied_now: list[str] = []
    try:
        await conn.execute("""create table if not exists schema_migrations (
            filename text primary key, sha256 text not null, applied_at timestamptz not null default now())""")
        await conn.execute("select pg_advisory_lock(727171)")
        done = {r["filename"]: r["sha256"] for r in await conn.fetch("select filename, sha256 from schema_migrations")}
        for name, digest, sql in _files(directory):
            if name in done:
                if done[name] != digest:
                    raise RuntimeError(f"applied migration {name} was modified; create a new migration instead")
                continue
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("insert into schema_migrations(filename, sha256) values ($1, $2)", name, digest)
            except asyncpg.PostgresError as exc:
                raise RuntimeError(f"migration {name} failed and was rolled back: {exc}") from exc
            applied_now.append(name)
        return applied_now
    finally:
        try:
            await conn.execute("select pg_advisory_unlock(727171)")
        finally:
            await conn.close()


def _describe_connect_error(exc: BaseException, dsn: str) -> str:
    from urllib.parse import urlsplit
    u = urlsplit(dsn)
    where = f"{u.hostname}:{u.port or 5432}/{(u.path or '/').lstrip('/')} as {u.username}"
    if isinstance(exc, asyncpg.InvalidPasswordError):
        return f"password authentication failed for {where}: DATABASE_URL does not match the database's credentials"
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return f"database does not exist at {where}: check the database name in DATABASE_URL"
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return f"cannot connect to PostgreSQL at {where} ({exc}): is the database running?"
    return f"{type(exc).__name__}: {exc}"


def main() -> None:
    from jarvis.config import get_settings

    dsn = get_settings().database_url
    cmd = sys.argv[1] if len(sys.argv) > 1 else "apply"
    try:
        if cmd == "status":
            st = asyncio.run(status(dsn))
            print("migrations:", st.describe())
            sys.exit(0 if st.ok else 3)
        if cmd != "apply":
            raise SystemExit("usage: python -m jarvis.db.migrate [apply|status]")
        applied = asyncio.run(migrate(dsn))
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        print("migration error:", _describe_connect_error(exc, dsn), file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print("migration error:", exc, file=sys.stderr)
        sys.exit(1)
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    sys.exit(0)


if __name__ == "__main__":
    main()

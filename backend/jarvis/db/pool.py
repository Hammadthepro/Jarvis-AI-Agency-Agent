"""asyncpg pool with health monitoring and reconnection with exponential backoff.

When the database is unhealthy the API returns 503 and the worker stops claiming work, so no
billable call can run without a ledger write."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import structlog

from jarvis.core.errors import DatabaseUnavailable

log = structlog.get_logger(__name__)


async def _init_conn(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: str, *, pool_size: int = 5, max_backoff_s: int = 60) -> None:
        self.dsn = dsn
        self.pool_size = pool_size
        self.max_backoff_s = max_backoff_s
        self._pool: asyncpg.Pool | None = None
        self.state = "unknown"          # healthy | unreachable | paused_suspected
        self.last_error: str | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=self.pool_size,
                                                   init=_init_conn, command_timeout=60)
            self.state = "healthy"

    async def connect_with_retry(self, stop: asyncio.Event | None = None) -> None:
        delay = 1.0
        while True:
            try:
                await self.connect()
                return
            except (OSError, asyncpg.PostgresError) as exc:
                self._mark_down(exc)
                log.warning("db.connect_failed", error=str(exc), retry_in_s=delay)
                if stop is not None and stop.is_set():
                    raise
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, self.max_backoff_s)

    def _mark_down(self, exc: BaseException) -> None:
        msg = str(exc)
        self.last_error = msg
        self.state = "paused_suspected" if "paused" in msg.lower() or "tenant" in msg.lower() else "unreachable"

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def health(self) -> bool:
        try:
            if self._pool is None:
                await self.connect()
            assert self._pool is not None
            async with self._pool.acquire(timeout=5) as conn:
                await conn.fetchval("select 1")
            self.state, self.last_error = "healthy", None
            return True
        except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            self._mark_down(exc)
            await self._reset()
            return False

    async def _reset(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.terminate()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        if self._pool is None:
            try:
                await self.connect()
            except (OSError, asyncpg.PostgresError) as exc:
                self._mark_down(exc)
                raise DatabaseUnavailable("database unavailable") from exc
        assert self._pool is not None
        try:
            async with self._pool.acquire(timeout=10) as conn:
                yield conn
        except (OSError, asyncpg.ConnectionDoesNotExistError, asyncpg.CannotConnectNowError,
                asyncpg.InterfaceError) as exc:
            self._mark_down(exc)
            await self._reset()
            raise DatabaseUnavailable("database unavailable") from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.acquire() as conn, conn.transaction():
            yield conn

    async def fetch(self, sql: str, *args) -> list[asyncpg.Record]:
        async with self.acquire() as c:
            return await c.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args) -> asyncpg.Record | None:
        async with self.acquire() as c:
            return await c.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        async with self.acquire() as c:
            return await c.fetchval(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        async with self.acquire() as c:
            return await c.execute(sql, *args)

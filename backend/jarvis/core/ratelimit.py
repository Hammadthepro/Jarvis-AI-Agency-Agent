"""Shared, persistent rate limiting (token bucket in PostgreSQL).

One atomic upsert per attempt: the bucket row is locked by ON CONFLICT DO UPDATE, refilled from the
elapsed time, and debited only if enough tokens remain. Because state lives in Postgres, limits hold
across every API instance and worker process. If the database is unavailable the caller fails
(DatabaseUnavailable), never silently allowing traffic."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jarvis.db.pool import Database

_TAKE = """
insert into rate_limit_buckets as b (bucket_key, tokens, capacity, refill_per_s, updated_at)
select $1::text, $2::float8 - $4::float8, $2::float8, $3::float8, clock_timestamp() where $4::float8 <= $2::float8
on conflict (bucket_key) do update set
  tokens = least(excluded.capacity, b.tokens + extract(epoch from clock_timestamp() - b.updated_at) * excluded.refill_per_s) - $4::float8,
  capacity = excluded.capacity, refill_per_s = excluded.refill_per_s, updated_at = clock_timestamp()
where least(excluded.capacity, b.tokens + extract(epoch from clock_timestamp() - b.updated_at) * excluded.refill_per_s) >= $4::float8
returning tokens
"""
_PEEK = """select least(capacity, tokens + extract(epoch from clock_timestamp() - updated_at) * refill_per_s) from rate_limit_buckets
           where bucket_key = $1"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float


class PgRateLimiter:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def take(self, key: str, *, capacity: float, refill_per_s: float, cost: float = 1.0) -> Decision:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(_TAKE, key, float(capacity), float(refill_per_s), float(cost))
            if row is not None:
                return Decision(True, 0.0)
            avail = await conn.fetchval(_PEEK, key) or 0.0
        wait = (cost - avail) / refill_per_s if refill_per_s > 0 else float("inf")
        return Decision(False, max(0.0, wait))


@lru_cache
def provider_rate_limits() -> dict[str, dict]:
    data = json.loads((Path(__file__).resolve().parents[1] / "resources" / "default_limits.json").read_text())
    return data.get("rate_limits", {})

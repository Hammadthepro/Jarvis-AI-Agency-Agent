"""PostgreSQL-backed durable task queue.

Delivery is AT-LEAST-ONCE: a task whose lease expires (worker crash) is re-claimed. Handlers must
therefore make external effects idempotent (idempotency keys, checkpoints, outbox). Claiming uses
`FOR UPDATE SKIP LOCKED` so concurrent workers never claim the same task."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg

from jarvis.db.audit import emit
from jarvis.db.pool import Database


@dataclass(frozen=True)
class Task:
    id: UUID
    workspace_id: UUID
    campaign_id: UUID | None
    task_type: str
    subject_id: UUID | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str

    @classmethod
    def from_row(cls, r: asyncpg.Record) -> Task:
        return cls(r["id"], r["workspace_id"], r["campaign_id"], r["task_type"], r["subject_id"],
                   r["payload"], r["attempts"], r["max_attempts"], r["idempotency_key"])


def backoff_seconds(attempt: int, *, base: float = 5.0, cap: float = 900.0) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * (2 ** max(0, attempt - 1))))


async def enqueue(conn: asyncpg.Connection, *, workspace_id: UUID, task_type: str, idempotency_key: str,
                  campaign_id: UUID | None = None, subject_id: UUID | None = None, payload: dict | None = None,
                  parent_task_id: UUID | None = None, priority: int = 0, max_attempts: int = 5,
                  delay_s: float = 0) -> UUID:
    """Idempotent: enqueueing the same key twice returns the existing task."""
    row = await conn.fetchrow(
        """insert into tasks(workspace_id, campaign_id, parent_task_id, task_type, subject_id, payload,
                             idempotency_key, priority, max_attempts, run_after)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9, now() + make_interval(secs => $10))
           on conflict (idempotency_key) do nothing returning id""",
        workspace_id, campaign_id, parent_task_id, task_type, subject_id, payload or {}, idempotency_key,
        priority, max_attempts, float(delay_s))
    if row:
        return row["id"]
    return await conn.fetchval("select id from tasks where idempotency_key = $1", idempotency_key)


async def claim(db: Database, worker_id: str, lease_s: int, task_types: list[str] | None = None) -> Task | None:
    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """update tasks t set status = 'running', leased_by = $1, attempts = t.attempts + 1,
                      lease_expires_at = now() + make_interval(secs => $2), started_at = coalesce(t.started_at, now())
               where t.id = (
                 select id from tasks
                 where status in ('pending', 'retry_wait') and run_after <= now() and not cancel_requested
                   and ($3::text[] is null or task_type = any($3))
                   and not exists (select 1 from system_settings s where s.workspace_id = tasks.workspace_id
                                   and s.global_kill_switch)
                   and not exists (select 1 from campaigns c where c.id = tasks.campaign_id
                                   and (c.kill_switch or c.state in ('paused', 'cancelled')))
                 order by priority desc, run_after
                 for update skip locked limit 1)
               returning t.*""",
            worker_id, float(lease_s), task_types)
        return Task.from_row(row) if row else None


async def heartbeat(db: Database, task_id: UUID, worker_id: str, lease_s: int) -> bool:
    """Extends the lease. Returns False if the task was lost (reclaimed) or cancellation was requested."""
    row = await db.fetchrow(
        """update tasks set lease_expires_at = now() + make_interval(secs => $3)
           where id = $1 and leased_by = $2 and status = 'running' returning cancel_requested""",
        task_id, worker_id, float(lease_s))
    return bool(row) and not row["cancel_requested"]


async def complete(db: Database, task: Task, worker_id: str, result: dict | None = None) -> bool:
    status = await db.execute(
        """update tasks set status = 'succeeded', result = $3, finished_at = now(), lease_expires_at = null,
                  error_code = null, error_detail = null
           where id = $1 and leased_by = $2 and status = 'running'""", task.id, worker_id, result or {})
    return status.endswith("1")


async def fail(db: Database, task: Task, worker_id: str, *, code: str, detail: str, retryable: bool,
               blocked: bool = False) -> str:
    """Retry with backoff, mark blocked (needs a human/config change), or dead-letter."""
    async with db.transaction() as conn:
        if blocked:
            new_status, delay = "blocked", 0.0
        elif retryable and task.attempts < task.max_attempts:
            new_status, delay = "retry_wait", backoff_seconds(task.attempts)
        else:
            new_status, delay = "dead" if retryable else "failed", 0.0
        res = await conn.execute(
            """update tasks set status = $3::task_status, error_code = $4, error_detail = $5, lease_expires_at = null,
                      run_after = now() + make_interval(secs => $6),
                      finished_at = case when $3::text in ('failed','dead','blocked') then now() else null end
               where id = $1 and leased_by = $2 and status = 'running'""",
            task.id, worker_id, new_status, code, detail[:4000], delay)
        if not res.endswith("1"):
            return "lost"
        if new_status == "dead":
            await conn.execute("insert into dead_letters(task_id, reason, snapshot) values ($1,$2,$3)",
                               task.id, f"{code}: {detail[:500]}", {"payload": task.payload, "attempts": task.attempts})
        await emit(conn, workspace_id=task.workspace_id, task_id=task.id, campaign_id=task.campaign_id,
                   event_type="error" if new_status != "retry_wait" else "retry",
                   message=f"{task.task_type} {new_status}: {code}", data={"detail": detail[:500]})
        return new_status


async def release_cancelled(db: Database, task: Task, worker_id: str) -> None:
    await db.execute(
        """update tasks set status = 'cancelled', finished_at = now(), lease_expires_at = null
           where id = $1 and leased_by = $2""", task.id, worker_id)


async def recover_expired_leases(db: Database) -> int:
    """Tasks whose worker died: back to retry_wait (attempt already counted) or dead if exhausted."""
    rows = await db.fetch(
        """update tasks set status = case when attempts >= max_attempts then 'dead'::task_status else 'retry_wait'::task_status end,
                  leased_by = null, lease_expires_at = null,
                  error_code = 'LEASE_EXPIRED', error_detail = 'worker lease expired (crash or stall)'
           where status in ('leased', 'running') and lease_expires_at < now() returning id, status""")
    for r in rows:
        if r["status"] == "dead":
            await db.execute("insert into dead_letters(task_id, reason, snapshot) values ($1,'LEASE_EXPIRED','{}')", r["id"])
    return len(rows)


async def request_cancel(conn: asyncpg.Connection, *, workspace_id: UUID, campaign_id: UUID) -> int:
    res = await conn.execute(
        """update tasks set cancel_requested = true,
                  status = case when status in ('pending','retry_wait','blocked') then 'cancelled'::task_status else status end
           where workspace_id = $1 and campaign_id = $2 and status in ('pending','retry_wait','blocked','running','leased')""",
        workspace_id, campaign_id)
    return int(res.split()[-1])


async def save_checkpoint(conn: asyncpg.Connection, task_id: UUID, step: str, state: dict) -> None:
    await conn.execute(
        """insert into task_checkpoints(task_id, step, state) values ($1,$2,$3)
           on conflict (task_id, step) do update set state = excluded.state, created_at = now()""",
        task_id, step, state)


async def load_checkpoints(db: Database, task_id: UUID) -> dict[str, dict]:
    rows = await db.fetch("select step, state from task_checkpoints where task_id = $1", task_id)
    return {r["step"]: r["state"] for r in rows}


LEASE = timedelta(seconds=300)

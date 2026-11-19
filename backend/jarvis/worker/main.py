"""Persistent background worker.

* Claims tasks atomically (FOR UPDATE SKIP LOCKED); concurrency-limited; separate build concurrency.
* Heartbeats extend leases; a lost lease or cancel request stops the handler.
* Error mapping: retryable provider errors -> retry with backoff+jitter; compliance/budget/credential
  problems -> `blocked` (needs a human or configuration); everything else -> failed/dead-letter.
* DB health monitor: when the database is unreachable the worker stops claiming and reconnects
  with exponential backoff; no provider call can run without a ledger write.
* Scheduler (single-instance via advisory lock): lease recovery, preview expiry cleanup,
  retention purges, coordinate expiry.
* Graceful shutdown on SIGTERM/SIGINT: stop claiming, let in-flight tasks finish up to a grace
  period, then release their leases for another worker (at-least-once, never silently dropped)."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from datetime import UTC, datetime

import structlog

from jarvis.agents.deps import Deps
from jarvis.config import Settings, get_settings
from jarvis.core.errors import (
    BudgetBlocked,
    ComplianceBlocked,
    DatabaseUnavailable,
    IntegrationState,
    JarvisError,
    ProviderError,
)
from jarvis.db import queue
from jarvis.db.pool import Database
from jarvis.observability.logging import configure_logging
from jarvis.workflow import engine

log = structlog.get_logger(__name__)
VERSION = os.environ.get("JARVIS_VERSION", "dev")
BUILD_TYPES = {"site.build", "site.deploy"}
BLOCKING_STATES = {IntegrationState.NOT_CONFIGURED, IntegrationState.INVALID_CREDENTIALS, IntegrationState.PERMISSION_DENIED,
                   IntegrationState.QUOTA_EXCEEDED, IntegrationState.DISABLED, IntegrationState.BLOCKED_PENDING_APPROVAL}


class Worker:
    def __init__(self, deps: Deps, *, worker_id: str | None = None, concurrency: int | None = None,
                 poll_s: float = 1.0, grace_s: float = 60.0) -> None:
        self.deps = deps
        self.db = deps.db
        s: Settings = deps.settings
        self.worker_id = worker_id or s.jarvis_worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.lease_s = s.jarvis_task_lease_seconds
        self.sem = asyncio.Semaphore(concurrency or s.jarvis_worker_concurrency)
        self.build_sem = asyncio.Semaphore(1)
        self.poll_s = poll_s
        self.grace_s = grace_s
        self.stop = asyncio.Event()
        self.inflight: dict[uuid.UUID, asyncio.Task] = {}
        self.base_settings: Settings = s
        self._cred_version: str | None = None
        self.started = datetime.now(UTC)

    async def run_task(self, task: queue.Task) -> None:
        hb_stop = asyncio.Event()
        cancelled = asyncio.Event()

        async def heartbeats() -> None:
            while not hb_stop.is_set():
                try:
                    await asyncio.wait_for(hb_stop.wait(), timeout=max(5, self.lease_s / 3))
                except TimeoutError:
                    try:
                        alive = await queue.heartbeat(self.db, task.id, self.worker_id, self.lease_s)
                    except DatabaseUnavailable:
                        continue
                    if not alive:
                        cancelled.set()
                        runner.cancel()
                        return

        handler = engine.HANDLERS.get(task.task_type)
        runner = asyncio.current_task()
        hb = asyncio.create_task(heartbeats())
        await self._exec_record(task, "running")
        try:
            if handler is None:
                raise JarvisError(f"no handler for task type {task.task_type}")
            if task.task_type in BUILD_TYPES:
                async with self.build_sem:
                    result = await handler(self.deps, task)
            else:
                result = await handler(self.deps, task)
            await queue.complete(self.db, task, self.worker_id, result)
            await self._exec_record(task, "succeeded", output=result)
        except asyncio.CancelledError:
            if cancelled.is_set():
                await queue.release_cancelled(self.db, task, self.worker_id)
                await self._exec_record(task, "failed", error="cancelled or lease lost")
            else:  # shutdown: hand the lease back for another worker
                await self.db.execute("""update tasks set status='retry_wait', leased_by=null, lease_expires_at=null,
                                         run_after=now() where id=$1 and leased_by=$2""", task.id, self.worker_id)
            raise
        except (ComplianceBlocked, BudgetBlocked) as exc:
            await queue.fail(self.db, task, self.worker_id, code=exc.code, detail=exc.message, retryable=False, blocked=True)
            await self._exec_record(task, "failed", error=f"{exc.code}: {exc.message}")
        except ProviderError as exc:
            blocked = exc.state in BLOCKING_STATES
            await queue.fail(self.db, task, self.worker_id, code=exc.state.value, detail=exc.message,
                             retryable=exc.retryable and not blocked, blocked=blocked)
            await self._exec_record(task, "failed", error=exc.message)
        except DatabaseUnavailable:
            log.warning("task.db_unavailable", task_id=str(task.id))  # lease will expire and be recovered
        except Exception as exc:
            log.exception("task.crashed", task_id=str(task.id), task_type=task.task_type)
            await queue.fail(self.db, task, self.worker_id, code="INTERNAL_ERROR", detail=f"{type(exc).__name__}: {exc}",
                             retryable=True)
            await self._exec_record(task, "failed", error=str(exc)[:500])
        finally:
            hb_stop.set()
            await asyncio.gather(hb, return_exceptions=True)
        if task.campaign_id:
            try:
                await engine.advance(self.deps, task.campaign_id)
            except Exception:
                log.exception("advance.failed", campaign_id=str(task.campaign_id))

    async def _exec_record(self, task: queue.Task, status: str, output: dict | None = None, error: str | None = None) -> None:
        try:
            if status == "running":
                await self.db.execute("""insert into agent_executions(workspace_id, task_id, agent, status, input_ref)
                                         values ($1,$2,$3,'running',$4)""", task.workspace_id, task.id,
                                      task.task_type.split(".")[0], {"task_type": task.task_type, "attempt": task.attempts})
            else:
                await self.db.execute("""update agent_executions set status=$2, output_ref=$3, error=$4, finished_at=now()
                                         where id = (select id from agent_executions where task_id=$1 and status='running'
                                         order by started_at desc limit 1)""", task.id, status, output, error)
        except Exception:  # bookkeeping must never fail the task
            log.warning("agent_execution.record_failed", task_id=str(task.id))

    async def refresh_credentials(self) -> bool:
        """Applies credentials saved in the Integrations Center when they change (checked every heartbeat)."""
        from jarvis.core import integrations
        v = await integrations.credentials_version(self.db)
        if v == self._cred_version or (self._cred_version is None and v.endswith(":0")):
            self._cred_version = v
            return False
        res = await integrations.refresh(self.deps, self.base_settings)
        self._cred_version = v
        if res["error"]:
            log.error("credentials.load_failed", detail=res["error"])
        else:
            log.info("credentials.applied", from_dashboard=sorted(p for p, i in res["info"].items()
                                                                  if "dashboard" in i["sources"].values()))
        return True

    async def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            healthy = await self.db.health()
            if healthy:
                try:
                    await self.refresh_credentials()
                except Exception:   # never stop heartbeats; the error is logged
                    log.exception("credentials.refresh_failed")
            try:
                if healthy:
                    await self.db.execute(
                        """insert into worker_heartbeats(worker_id, started_at, last_seen_at, version, db_state)
                           values ($1,$2,now(),$3,'healthy') on conflict (worker_id) do update
                           set last_seen_at = now(), db_state = 'healthy', version = excluded.version""",
                        self.worker_id, self.started, VERSION)
            except DatabaseUnavailable:
                pass
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.deps.settings.db_health_interval_s)
            except TimeoutError:
                pass

    SCHEDULE = {"lease_recovery": 60, "retention": 3600, "usage_sync": 3600, "preview_cleanup": 3600}

    async def _job(self, name: str) -> None:
        from jarvis.agents.deployment import cleanup_expired
        from jarvis.core.retention import run_retention
        from jarvis.core.usage_sync import sync_provider_usage
        if name == "lease_recovery":
            n = await queue.recover_expired_leases(self.db)
            if n:
                log.info("leases.recovered", count=n)
        elif name == "retention":
            log.info("retention.done", **await run_retention(self.db))
        elif name == "usage_sync":
            log.info("usage_sync.done", results=await sync_provider_usage(self.deps))
        elif name == "preview_cleanup" and self.deps.cloudflare.configured:
            await cleanup_expired(self.deps)

    async def run_due_jobs(self) -> list[str]:
        """Runs every due job once. Only one worker runs jobs at a time: the advisory lock is taken and
        released on the SAME dedicated connection. Job timing is stored in scheduled_jobs."""
        ran: list[str] = []
        async with self.db.acquire() as lock_conn:
            if not await lock_conn.fetchval("select pg_try_advisory_lock(424242)"):
                return ran
            try:
                for name, every in self.SCHEDULE.items():
                    due = await self.db.fetchval(
                        """select coalesce((select last_started_at < now() - make_interval(secs => $2)
                                            from scheduled_jobs where name = $1), true)""", name, float(every))
                    if not due:
                        continue
                    await self.db.execute("""insert into scheduled_jobs(name, last_started_at) values ($1, now())
                                             on conflict (name) do update set last_started_at = now()""", name)
                    try:
                        await self._job(name)
                        status, detail = "succeeded", None
                    except DatabaseUnavailable:
                        raise
                    except Exception as exc:
                        log.exception("scheduler.job_failed", job=name)
                        status, detail = "failed", f"{type(exc).__name__}: {exc}"[:500]
                    await self.db.execute("""update scheduled_jobs set last_finished_at = now(), last_status = $2, last_detail = $3
                                             where name = $1""", name, status, detail)
                    ran.append(name)
            finally:
                await lock_conn.fetchval("select pg_advisory_unlock(424242)")
        return ran

    async def scheduler_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.run_due_jobs()
            except DatabaseUnavailable:
                pass
            except Exception:
                log.exception("scheduler.error")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30)
            except TimeoutError:
                pass

    async def claim_loop(self) -> None:
        while not self.stop.is_set():
            if self.db.state != "healthy":
                await asyncio.sleep(self.poll_s * 5)
                continue
            await self.sem.acquire()
            task = None
            try:
                # Kill switches (per workspace/campaign) are enforced inside the claim query.
                task = await queue.claim(self.db, self.worker_id, self.lease_s)
            except DatabaseUnavailable:
                task = None
            if task is None:
                self.sem.release()
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.poll_s)
                except TimeoutError:
                    pass
                continue
            t = asyncio.create_task(self._wrapped(task))
            self.inflight[task.id] = t

    async def _wrapped(self, task: queue.Task) -> None:
        try:
            await self.run_task(task)
        finally:
            self.inflight.pop(task.id, None)
            self.sem.release()

    async def serve(self) -> None:
        await self.db.connect_with_retry(self.stop)
        from jarvis.db.migrate import status_conn
        async with self.db.acquire() as conn:
            st = await status_conn(conn)
        if not st.ok:   # never run tasks against a schema the code does not match
            await self.db.close()
            raise SchemaNotReady(st.describe())
        loops = [asyncio.create_task(self.claim_loop()), asyncio.create_task(self.heartbeat_loop()),
                 asyncio.create_task(self.scheduler_loop())]
        await self.stop.wait()
        log.info("worker.stopping", inflight=len(self.inflight))
        for t in loops:
            t.cancel()
        if self.inflight:
            done, pending = await asyncio.wait(list(self.inflight.values()), timeout=self.grace_s)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.db.close()


class SchemaNotReady(RuntimeError):
    pass


def main() -> None:
    settings = get_settings()
    configure_logging(settings.jarvis_log_level)
    db = Database(settings.database_url, pool_size=settings.database_pool_size, max_backoff_s=settings.db_reconnect_max_backoff_s)
    worker = Worker(Deps.build(settings, db))

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop.set)
        await worker.serve()

    try:
        asyncio.run(runner())
    except SchemaNotReady as exc:
        log.error("worker.schema_not_ready", detail=str(exc))
        raise SystemExit(f"worker not started: database migrations are not applied ({exc})") from None


if __name__ == "__main__":
    main()

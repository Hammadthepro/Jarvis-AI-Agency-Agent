"""Shared HTTP plumbing and the ToolGateway through which EVERY billable provider call passes.

Gateway order: kill switches -> compliance gate -> budget reservation (row-locked) -> request ->
usage event + commit/release/ambiguous -> integration state update."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import structlog

from jarvis.core import budget
from jarvis.core.compliance import require_gate
from jarvis.core.errors import BudgetBlocked, ComplianceBlocked, IntegrationState, ProviderError
from jarvis.core.ratelimit import PgRateLimiter, provider_rate_limits
from jarvis.db.pool import Database

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


@dataclass
class CallContext:
    db: Database
    workspace_id: UUID
    task_id: UUID | None = None
    campaign_id: UUID | None = None
    site_id: UUID | None = None


@dataclass
class CallOutcome:
    value: Any
    units: int
    cost_micros: int
    sku: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    http_status: int | None = None
    extra: dict = field(default_factory=dict)


def map_http_error(provider: str, resp: httpx.Response) -> ProviderError:
    s = resp.status_code
    body = resp.text[:300]
    if s == 401:
        return ProviderError(provider, IntegrationState.INVALID_CREDENTIALS, body, http_status=s)
    if s == 403:
        return ProviderError(provider, IntegrationState.PERMISSION_DENIED, body, http_status=s)
    if s == 402:
        return ProviderError(provider, IntegrationState.QUOTA_EXCEEDED, body, http_status=s)
    if s == 429:
        quota = "quota" in body.lower() or "exhaust" in body.lower()
        return ProviderError(provider, IntegrationState.QUOTA_EXCEEDED if quota else IntegrationState.RATE_LIMITED,
                             body, http_status=s, retryable=not quota)
    if s >= 500:
        return ProviderError(provider, IntegrationState.TEMPORARILY_UNAVAILABLE if s in (502, 503, 504)
                             else IntegrationState.PROVIDER_ERROR, body, http_status=s, retryable=True)
    return ProviderError(provider, IntegrationState.PROVIDER_ERROR, body, http_status=s, retryable=False)


async def http_request(provider: str, client: httpx.AsyncClient, method: str, url: str, *,
                       retries: int = 2, **kw) -> httpx.Response:
    """Bounded retries only for errors where the request provably did not reach the provider
    (connect errors) or the provider asked us to retry (429/503 with no billing). Read timeouts are
    NOT retried here and are reported as billing-ambiguous."""
    attempt = 0
    while True:
        try:
            resp = await client.request(method, url, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if attempt < retries:
                attempt += 1
                await asyncio.sleep(random.uniform(0.2, 1.0) * 2 ** attempt)
                continue
            raise ProviderError(provider, IntegrationState.TEMPORARILY_UNAVAILABLE, f"connect failed: {exc}",
                                retryable=True) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ProviderError(provider, IntegrationState.TEMPORARILY_UNAVAILABLE,
                                f"no response after sending ({type(exc).__name__}); outcome unknown",
                                retryable=False, billing_ambiguous=True) from exc
        if resp.status_code in (429, 503) and attempt < retries:
            ra = resp.headers.get("retry-after")
            delay = float(ra) if ra and ra.replace(".", "").isdigit() else random.uniform(0.5, 1.5) * 2 ** attempt
            attempt += 1
            await asyncio.sleep(min(delay, 30))
            continue
        if resp.status_code >= 400:
            raise map_http_error(provider, resp)
        return resp


async def _set_state(db: Database, ws: UUID, provider: str, state: IntegrationState, detail: str | None) -> None:
    try:
        await db.execute(
            """insert into provider_integrations(workspace_id, provider, configured, state, detail, last_checked_at, last_test_ok)
               values ($1,$2,true,$3,$4, now(), $5)
               on conflict (workspace_id, provider) do update set state = excluded.state, detail = excluded.detail,
                  configured = true, last_checked_at = now(), last_test_ok = excluded.last_test_ok""",
            ws, provider, state.value, detail, state == IntegrationState.CONNECTED)
    except Exception as exc:  # never mask the original provider outcome
        log.warning("integration_state.update_failed", error=str(exc))


async def check_kill_switches(db: Database, ctx: CallContext, provider: str) -> None:
    row = await db.fetchrow("select global_kill_switch, provider_kill_switches from system_settings where workspace_id = $1",
                            ctx.workspace_id)
    if row and row["global_kill_switch"]:
        raise ProviderError(provider, IntegrationState.DISABLED, "global kill switch is on")
    if row and row["provider_kill_switches"].get(provider):
        raise ProviderError(provider, IntegrationState.DISABLED, f"{provider} kill switch is on")
    if ctx.campaign_id:
        ks = await db.fetchval("select kill_switch from campaigns where id = $1", ctx.campaign_id)
        if ks:
            raise ProviderError(provider, IntegrationState.DISABLED, "campaign kill switch is on")


async def gateway_call(ctx: CallContext, *, provider: str, operation: str, unit: str, units: int,
                       estimated_micros: int, fn: Callable[[], Awaitable[CallOutcome]],
                       compliance_scope: str | None = None) -> Any:
    db = ctx.db
    await check_kill_switches(db, ctx, provider)
    rl = provider_rate_limits().get(provider)
    if rl:
        d = await PgRateLimiter(db).take(f"provider:{provider}", capacity=rl["capacity"],
                                         refill_per_s=rl["capacity"] / rl["per_seconds"])
        if not d.allowed:   # checked before any reservation: nothing is sent, nothing is reserved
            raise ProviderError(provider, IntegrationState.RATE_LIMITED,
                                f"shared throughput limit reached; retry in {d.retry_after_s:.0f}s", retryable=True)
    pv = budget.pricing()["version"]
    if compliance_scope:
        async with db.acquire() as conn:
            try:
                await require_gate(conn, ctx.workspace_id, compliance_scope)
            except ComplianceBlocked:
                await conn.execute(
                    """insert into usage_events(workspace_id, task_id, campaign_id, provider, operation, outcome, pricing_version)
                       values ($1,$2,$3,$4,$5,'blocked',$6)""", ctx.workspace_id, ctx.task_id, ctx.campaign_id,
                    provider, operation, pv)
                raise
    async with db.transaction() as conn:
        try:
            res = await budget.reserve(conn, workspace_id=ctx.workspace_id, provider=provider, unit=unit, units=units,
                                       amount_micros=estimated_micros, task_id=ctx.task_id,
                                       campaign_id=ctx.campaign_id, site_id=ctx.site_id)
        except BudgetBlocked:
            raise
    t0 = time.monotonic()
    try:
        out = await fn()
    except ProviderError as exc:
        latency = int((time.monotonic() - t0) * 1000)
        async with db.transaction() as conn:
            if exc.billing_ambiguous:
                await budget.mark_ambiguous(conn, res)
            else:
                await budget.release(conn, res)
            await conn.execute(
                """insert into usage_events(workspace_id, task_id, campaign_id, site_id, reservation_id, provider, operation,
                       units, outcome, http_status, latency_ms, estimated_cost_micros, pricing_version)
                   values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                ctx.workspace_id, ctx.task_id, ctx.campaign_id, ctx.site_id, res.id, provider, operation,
                units if exc.billing_ambiguous else 0, "ambiguous" if exc.billing_ambiguous else "error",
                exc.upstream_status, latency, estimated_micros if exc.billing_ambiguous else 0, pv)
        await _set_state(db, ctx.workspace_id, provider, exc.state, exc.message[:300])
        raise
    except BaseException:
        # Unknown failure after the call may have been sent: keep the reservation counted.
        async with db.transaction() as conn:
            await budget.mark_ambiguous(conn, res)
        raise
    latency = int((time.monotonic() - t0) * 1000)
    async with db.transaction() as conn:
        await budget.commit(conn, res, actual_units=out.units, actual_micros=out.cost_micros)
        await conn.execute(
            """insert into usage_events(workspace_id, task_id, campaign_id, site_id, reservation_id, provider, operation, sku,
                   model, units, tokens_in, tokens_out, outcome, http_status, latency_ms, estimated_cost_micros, pricing_version)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'ok',$13,$14,$15,$16)""",
            ctx.workspace_id, ctx.task_id, ctx.campaign_id, ctx.site_id, res.id, provider, operation, out.sku,
            out.model, out.units, out.tokens_in, out.tokens_out, out.http_status, latency, out.cost_micros, pv)
    await _set_state(db, ctx.workspace_id, provider, IntegrationState.CONNECTED, None)
    return out.value

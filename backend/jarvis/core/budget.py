"""Cost reservation engine.

Every billable call reserves its WORST-CASE units/cost before the request is sent. The reservation
is checked against every applicable limit (global/provider/campaign/site, day/month/total) under a
row lock, so concurrent reservations can never exceed a cap. Outcomes:

* commit    - provider confirmed; actual units/cost recorded
* release   - the request was provably not billed (never sent, or rejected before processing)
* ambiguous - timeout / connection loss after sending: billing unknown, the reservation stays
              counted against limits until reconciled. A timeout does not prove a call was free.

No limit row for the workspace's global spend => fail closed (BUDGET_BLOCKED)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import asyncpg

from jarvis.core.errors import BudgetBlocked

RES = Path(__file__).resolve().parents[1] / "resources"


@lru_cache
def pricing() -> dict:
    data = json.loads((RES / "pricing.json").read_text())
    override = os.environ.get("PRICING_OVERRIDES_JSON")
    if override:
        for prov, vals in json.loads(override).items():
            data.setdefault(prov, {}).update(vals)
        data["version"] = data["version"] + "+override"
    return data


def unit_price(provider: str, unit: str) -> int:
    return int(pricing()[provider][unit])


def claude_cost(model: str, tokens_in: int, tokens_out: int) -> int:
    p = pricing()["anthropic"][model]
    return (tokens_in * p["input_per_mtok"] + tokens_out * p["output_per_mtok"]) // 1_000_000


@dataclass(frozen=True)
class Reservation:
    id: UUID
    provider: str
    unit: str
    units: int
    amount_micros: int


async def seed_default_limits(conn: asyncpg.Connection, workspace_id: UUID) -> None:
    defaults = json.loads((RES / "default_limits.json").read_text())
    for lim in defaults["limits"]:
        await conn.execute(
            """insert into budget_limits(workspace_id, scope, provider, unit, period, cap, free_allowance)
               values ($1,$2,$3,$4,$5,$6,$7) on conflict do nothing""",
            workspace_id, lim["scope"], lim["provider"], lim["unit"], lim["period"], lim["cap"],
            lim.get("free_allowance"))


def _period_start_sql(period: str) -> str:
    return {"day": "date_trunc('day', now())", "month": "date_trunc('month', now())",
            "total": "'-infinity'::timestamptz"}[period]


async def reserve(conn: asyncpg.Connection, *, workspace_id: UUID, provider: str, unit: str, units: int,
                  amount_micros: int, task_id: UUID | None = None, campaign_id: UUID | None = None,
                  site_id: UUID | None = None) -> Reservation:
    """Must run inside a transaction. Raises BudgetBlocked without creating a reservation."""
    limits = await conn.fetch("select * from budget_limits where workspace_id = $1 order by id for update", workspace_id)
    if not any(lim["scope"] == "global" and lim["unit"] == "usd_micros" for lim in limits):
        raise BudgetBlocked("no global spend limit configured; set one in Settings before billable calls run")

    for lim in limits:
        applies, amount, where, args = False, 0, "", []
        if lim["scope"] == "global" and lim["unit"] == "usd_micros":
            applies, amount = True, amount_micros
        elif lim["scope"] == "provider" and lim["provider"] == provider:
            if lim["unit"] == "usd_micros":
                applies, amount, where, args = True, amount_micros, " and provider = $3", [provider]
            elif lim["unit"] == unit:
                applies, amount, where, args = True, units, " and provider = $3 and unit = $4", [provider, unit]
        if not applies:
            continue
        col = "units" if lim["unit"] != "usd_micros" else "amount_micros"
        used = await conn.fetchval(
            f"""select coalesce(sum({col}), 0) from cost_reservations
                where workspace_id = $1 and status in ('reserved','committed','ambiguous')
                  and created_at >= {_period_start_sql(lim['period'])} and $2::int = $2::int{where}""",
            workspace_id, 0, *args)
        if used + amount > lim["cap"]:
            raise BudgetBlocked(
                f"{lim['scope']} limit reached: {provider}/{lim['unit']} per {lim['period']} "
                f"(used {used}, requested {amount}, cap {lim['cap']})",
                details={"limit_id": str(lim["id"]), "used": used, "requested": amount, "cap": lim["cap"]})

    if campaign_id is not None:
        row = await conn.fetchrow("select budget_cap_micros from campaigns where id = $1 for update", campaign_id)
        used = await conn.fetchval(
            """select coalesce(sum(amount_micros),0) from cost_reservations
               where campaign_id = $1 and status in ('reserved','committed','ambiguous')""", campaign_id)
        if row and used + amount_micros > row["budget_cap_micros"]:
            raise BudgetBlocked(f"campaign budget reached (used {used}, requested {amount_micros}, cap {row['budget_cap_micros']})")
    if site_id is not None:
        cap = await conn.fetchval(
            """select c.per_site_cap_micros from generated_sites s join campaigns c on c.id = s.campaign_id
               where s.id = $1""", site_id)
        used = await conn.fetchval(
            """select coalesce(sum(amount_micros),0) from cost_reservations
               where site_id = $1 and status in ('reserved','committed','ambiguous')""", site_id)
        if cap is not None and used + amount_micros > cap:
            raise BudgetBlocked(f"per-site budget reached (used {used}, requested {amount_micros}, cap {cap})")

    rid = await conn.fetchval(
        """insert into cost_reservations(workspace_id, task_id, campaign_id, site_id, provider, unit, units,
                                         amount_micros, estimated_micros, status)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$8,'reserved') returning id""",
        workspace_id, task_id, campaign_id, site_id, provider, unit, units, amount_micros)
    return Reservation(rid, provider, unit, units, amount_micros)


async def commit(conn: asyncpg.Connection, res: Reservation, *, actual_units: int, actual_micros: int) -> None:
    row = await conn.fetchrow(
        """update cost_reservations set status = 'committed', units = $2, amount_micros = $3, settled_at = now()
           where id = $1 and status in ('reserved','ambiguous')
           returning workspace_id, campaign_id, site_id, provider""", res.id, actual_units, actual_micros)
    if row:
        await conn.execute(
            """insert into cost_ledger(workspace_id, reservation_id, campaign_id, site_id, provider, entry_type, amount_micros)
               values ($1,$2,$3,$4,$5,'commit',$6)""",
            row["workspace_id"], res.id, row["campaign_id"], row["site_id"], row["provider"], actual_micros)
        if row["site_id"]:
            await conn.execute("update generated_sites set cost_micros = cost_micros + $2 where id = $1",
                               row["site_id"], actual_micros)


async def release(conn: asyncpg.Connection, res: Reservation) -> None:
    """Only call when the request was provably not billed."""
    await conn.execute("update cost_reservations set status = 'released', settled_at = now() where id = $1 and status = 'reserved'", res.id)


async def mark_ambiguous(conn: asyncpg.Connection, res: Reservation) -> None:
    await conn.execute("update cost_reservations set status = 'ambiguous' where id = $1 and status = 'reserved'", res.id)

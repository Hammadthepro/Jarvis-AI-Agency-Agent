"""Discovery providers: status per provider and the controlled provider evaluation (aggregates only)."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from jarvis.api.auth import CurrentPrincipal, Principal
from jarvis.core import provider_policy
from jarvis.core.errors import NotFound, ValidationFailed
from jarvis.db.audit import audit
from jarvis.db.queue import enqueue
from jarvis.discovery import evaluation

router = APIRouter()


def _j(x):
    return json.loads(json.dumps(x, default=str))


@router.get("/discovery/providers")
async def providers(request: Request, me: Principal = CurrentPrincipal):
    deps = request.app.state.deps
    async with deps.db.acquire() as conn:
        opts = await provider_policy.discovery_options(conn, deps, me.workspace_id)
    last = await deps.db.fetchrow("""select id, status, providers, created_at, finished_at from provider_evaluations
                                     where workspace_id=$1 order by created_at desc limit 1""", me.workspace_id)
    return _j({"providers": [{"provider": o["provider"], "label": o["label"], "available": o["allowed"],
                              "reason": o["reason"], "fix": o["fix"], "basis": o["basis"]} for o in opts],
               "last_evaluation": dict(last) if last else None})


class EvaluationIn(BaseModel):
    providers: list[str] = Field(min_length=1, max_length=3)
    cities: list[dict] = Field(min_length=1, max_length=3)
    categories: list[str] = Field(min_length=1, max_length=3)
    limit: int = Field(20, ge=1, le=20)
    confirm_cost: bool = False


@router.post("/discovery/evaluations")
async def start_evaluation(body: EvaluationIn, request: Request, me: Principal = CurrentPrincipal):
    """Runs the comparison in the durable worker. Requests are real and may use paid units, so the owner must confirm."""
    me.require("owner")
    bad = [p for p in body.providers if p not in evaluation.PROVIDERS]
    if bad:
        raise ValidationFailed(f"unknown provider(s): {', '.join(bad)}")
    if not body.confirm_cost:
        raise ValidationFailed("this sends real requests to each provider (bounded: at most 3 cities x 3 categories); "
                               "set confirm_cost=true to run it", details={"requires": "confirm_cost"})
    plan = evaluation.validate_plan({"cities": body.cities, "categories": body.categories, "limit": body.limit})
    db = request.app.state.deps.db
    async with db.transaction() as conn:
        eid = await conn.fetchval("""insert into provider_evaluations(workspace_id, created_by, providers, plan)
                                     values ($1,$2,$3,$4) returning id""", me.workspace_id, me.user_id, body.providers, plan)
        await enqueue(conn, workspace_id=me.workspace_id, task_type="provider.evaluate", idempotency_key=f"eval:{eid}",
                      subject_id=eid, payload={"providers": body.providers, "plan": plan}, max_attempts=1)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id),
                    action="discovery.evaluation_started", entity_type="provider_evaluation", entity_id=eid,
                    data={"providers": body.providers, "plan": plan})
    return {"evaluation_id": str(eid), "status": "running"}


@router.get("/discovery/evaluations")
async def list_evaluations(request: Request, me: Principal = CurrentPrincipal):
    rows = await request.app.state.deps.db.fetch(
        """select id, status, providers, plan, results, created_at, finished_at from provider_evaluations
           where workspace_id=$1 order by created_at desc limit 10""", me.workspace_id)
    return _j([dict(r) for r in rows])


@router.get("/discovery/evaluations/{evaluation_id}")
async def get_evaluation(evaluation_id: UUID, request: Request, me: Principal = CurrentPrincipal):
    r = await request.app.state.deps.db.fetchrow(
        "select * from provider_evaluations where id=$1 and workspace_id=$2", evaluation_id, me.workspace_id)
    if r is None:
        raise NotFound("evaluation not found")
    return _j(dict(r))

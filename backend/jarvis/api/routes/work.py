"""Work items (the owner's "tasks"): list, detail with real timings and activity, control, and website actions."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from jarvis.api.auth import CurrentPrincipal, Principal
from jarvis.core.errors import InvalidTransition, NotFound
from jarvis.db.audit import audit, emit
from jarvis.db.queue import enqueue
from jarvis.workflow import work

router = APIRouter()


@router.get("/work")
async def list_work(request: Request, me: Principal = CurrentPrincipal, limit: int = 20):
    return await work.list_work(request.app.state.db, me.workspace_id, limit)


@router.get("/work/{task_id}")
async def work_detail(task_id: UUID, request: Request, me: Principal = CurrentPrincipal):
    return await work.summary(request.app.state.db, me.workspace_id, task_id, detail=True)


@router.post("/work/{task_id}/{action}")
async def work_action(task_id: UUID, action: str, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    return await work.control(request.app.state.db, me, task_id, action)


class BuildIn(BaseModel):
    from_task_id: UUID | None = None
    prospect_ids: list[UUID] | None = Field(None, max_length=10)
    count: int = Field(1, ge=1, le=10)


@router.post("/work/website-build")
async def website_build(body: BuildIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    return await work.start_website_build(request.app.state.deps, me, parent_id=body.from_task_id,
                                          prospect_ids=body.prospect_ids, count=body.count)


async def _site(db, me, site_id):
    g = await db.fetchrow("""select g.*, c.state::text campaign_state from generated_sites g join campaigns c on c.id=g.campaign_id
                             where g.id=$1 and g.workspace_id=$2""", site_id, me.workspace_id)
    if g is None:
        raise NotFound("website not found")
    return g


@router.post("/sites/{site_id}/{action}")
async def site_action(site_id: UUID, action: str, request: Request, me: Principal = CurrentPrincipal):
    """rebuild: a new version (strategy, build, QA, repairs) and then a private preview deployment.
    qa: QA the current version again in the isolated sandbox. deploy: deploy the current QA-passed version."""
    me.require("operator")
    if action not in ("rebuild", "qa", "deploy"):
        raise NotFound("unknown action")
    db = request.app.state.db
    g = await _site(db, me, site_id)
    busy = await db.fetchval("""select count(*) from tasks where subject_id=$1 and task_type in ('site.build','site.qa','site.deploy')
                                and status in ('pending','leased','running','retry_wait')""", site_id)
    if busy:
        raise InvalidTransition("this website already has an operation in progress")
    if action in ("qa", "deploy") and not g["current_version_id"] and action == "deploy":
        raise InvalidTransition("this website has no QA-passed version to deploy yet")
    if action == "qa" and not await db.fetchval("select count(*) from site_versions where site_id=$1", site_id):
        raise InvalidTransition("this website has not been built yet")
    if action == "deploy" and g["state"] not in ("passed", "deployed"):
        raise InvalidTransition("only a website that passed QA can be deployed")
    n = await db.fetchval("select count(*) from tasks where subject_id=$1", site_id)
    task_type = {"rebuild": "site.build", "qa": "site.qa", "deploy": "site.deploy"}[action]
    async with db.transaction() as conn:
        if action == "rebuild":
            await conn.execute("update generated_sites set state='queued', last_error=null where id=$1", site_id)
        tid = await enqueue(conn, workspace_id=me.workspace_id, campaign_id=g["campaign_id"], task_type=task_type,
                            subject_id=site_id, idempotency_key=f"site:{site_id}:{action}:{n + 1}",
                            payload={"manual": True, "then_deploy": action == "rebuild"}, max_attempts=3 if action != "qa" else 1,
                            priority=7)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action=f"site.{action}",
                    entity_type="generated_site", entity_id=site_id)
        await emit(conn, workspace_id=me.workspace_id, campaign_id=g["campaign_id"], event_type="state",
                   message={"rebuild": "Website rebuild queued", "qa": "QA run queued", "deploy": "Deployment queued"}[action])
    return {"queued": action, "task_id": str(tid) if tid else None}

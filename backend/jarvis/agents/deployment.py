"""Agent 9: Deployment to Cloudflare Pages (Direct Upload), then verification.

A deployment is marked `verified` only when (1) Cloudflare's API reports the deployment and (2) the
pages.dev URL responds 200 with the expected title and the noindex header. Otherwise it stays
`uploaded`/`failed` and nothing downstream (outreach) may reference it."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from jarvis.agents.deps import Deps
from jarvis.core.errors import ProviderError
from jarvis.db.audit import audit
from jarvis.integrations.base import CallContext


def preview_branch(slug: str) -> str:
    """One Cloudflare Pages branch per site: <niche prefix>-<site's unique suffix>, at most 28 characters. The unique
    suffix is always kept, so two prospects never share a branch (and one preview never replaces another's)."""
    clean = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", slug.lower())).strip("-")
    head, _, suffix = clean.rpartition("-")
    if not head or not suffix:
        return clean[:28].strip("-")
    return f"{head[:27 - len(suffix)].strip('-')}-{suffix}"


async def deploy_site(deps: Deps, ctx: CallContext, site_id: UUID, verify_attempts: int = 5, verify_delay_s: float = 24) -> dict:
    db = deps.db
    row = await db.fetchrow(
        """select g.slug, g.state, v.id vid, v.artifact_path from generated_sites g join site_versions v on v.id = g.current_version_id
           where g.id = $1""", site_id)
    if not row or row["state"] not in ("passed", "deployed"):
        return {"status": "skipped", "reason": "site has not passed QA"}
    existing = await db.fetchrow("select * from deployments where site_version_id = $1 and status = 'verified'", row["vid"])
    if existing:
        return {"status": "verified", "url": existing["url"], "deployment_id": existing["provider_deployment_id"]}
    branch = preview_branch(row["slug"])
    # Restart safety: a retried task must not upload again if a previous attempt already did.
    prior = await db.fetchrow("""select * from deployments where site_version_id = $1
                                 and status in ('pending','uploaded','unknown_provider_result') order by created_at desc limit 1""", row["vid"])
    url = None
    if prior and prior["status"] == "uploaded" and prior["url"]:
        dep_id, url = prior["id"], prior["url"]
    elif prior:
        dep_id = prior["id"]
        existing_cf = await deps.cloudflare.latest_deployment(branch)   # did the crashed attempt reach Cloudflare?
        if existing_cf and existing_cf.get("url"):
            url = existing_cf["url"]
            await db.execute("""update deployments set status='uploaded', provider_deployment_id=$2, url=$3, upload_completed_at=now()
                                where id=$1""", dep_id, existing_cf.get("id"), url)
    else:
        dep_id = await db.fetchval(
            """insert into deployments(workspace_id, site_version_id, project_name, branch, status, expires_at)
               values ($1,$2,$3,$4,'pending',$5) returning id""", ctx.workspace_id, row["vid"], deps.cloudflare.project, branch,
            datetime.now(UTC) + timedelta(days=deps.settings.preview_ttl_days))
    if url is None:
        try:
            res = await deps.cloudflare.deploy(ctx, Path(row["artifact_path"]), branch)
        except ProviderError as exc:
            await db.execute("update deployments set status = $2, verification = $3 where id = $1", dep_id,
                             "unknown_provider_result" if exc.billing_ambiguous else "failed", {"error": exc.message[:500]})
            raise
        url = res.alias_url or res.url
        await db.execute("""update deployments set status='uploaded', provider_deployment_id=$2, url=$3, upload_completed_at=now()
                            where id=$1""", dep_id, res.deployment_id, url)
    deployment_id = await db.fetchval("select provider_deployment_id from deployments where id=$1", dep_id)
    title = re.search(r"<title>(.*?)</title>", Path(row["artifact_path"], "index.html").read_text(), re.S)
    expected = (title.group(1) if title else "").replace("&amp;", "&")
    checks = []
    for attempt in range(verify_attempts):
        fr = await deps.fetch_url(url)
        ok = fr.status == 200 and expected[:30] in fr.body.replace("&amp;", "&") and "noindex" in fr.headers.get("x-robots-tag", "")
        checks.append({"attempt": attempt + 1, "status": fr.status, "error": fr.error, "title_ok": expected[:30] in fr.body,
                       "noindex": "noindex" in fr.headers.get("x-robots-tag", "")})
        if ok:
            async with db.transaction() as conn:
                await conn.execute("update deployments set status='verified', verification=$2 where id=$1", dep_id, {"checks": checks})
                await conn.execute("update generated_sites set state='deployed' where id=$1", site_id)
                await audit(conn, workspace_id=ctx.workspace_id, actor_type="agent", actor_id="deployment",
                            action="deployment.verified", entity_type="deployment", entity_id=dep_id, data={"url": url})
            return {"status": "verified", "url": url, "deployment_id": deployment_id}
        await asyncio.sleep(verify_delay_s)
    await db.execute("update deployments set status='failed', verification=$2 where id=$1", dep_id, {"checks": checks})
    return {"status": "failed", "url": url, "reason": "deployed URL did not verify", "checks": checks}


async def cleanup_expired(deps: Deps) -> int:
    rows = await deps.db.fetch(
        """select d.id, d.workspace_id, d.provider_deployment_id from deployments d
           where d.status in ('verified','uploaded') and d.expires_at < now() limit 50""")
    n = 0
    for r in rows:
        try:
            if r["provider_deployment_id"]:
                await deps.cloudflare.delete_deployment(r["provider_deployment_id"])
        except ProviderError:
            continue
        await deps.db.execute("update deployments set status='deleted' where id=$1", r["id"])
        await deps.db.execute("""update generated_sites set state='expired' where current_version_id =
                                 (select site_version_id from deployments where id=$1)""", r["id"])
        n += 1
    return n

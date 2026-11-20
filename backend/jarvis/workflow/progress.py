"""Real, database-backed progress of a campaign, phrased for the owner, and proactive updates in the conversation.
Nothing here estimates: every number is a count of rows."""

from __future__ import annotations

from uuid import UUID


def _n(k: int, one: str, many: str | None = None) -> str:
    return f"{k} {one if k == 1 else (many or one + 's')}"


async def campaign_progress(db, workspace_id, campaign_id) -> dict:
    """Real progress of one campaign, from the database only. The sentence follows the stage the work is actually in."""
    c = await db.fetchrow("""select id, name, state, current_stage, blocked_reason, sites_to_build, target_count, discovery_provider,
                             niche from campaigns where id=$1 and workspace_id=$2""", campaign_id, workspace_id)
    if c is None:
        return {"found": False, "say": "I can't find that campaign."}
    n = await db.fetchrow(
        """select (select count(*) from prospects where campaign_id=$1) prospects,
                  (select count(*) from prospects where campaign_id=$1 and state not in ('rejected')) eligible,
                  (select count(*) from prospects where campaign_id=$1 and state not in ('rejected','discovered')) verified,
                  (select count(*) from prospects where campaign_id=$1 and state in ('researched','qualified')) researched,
                  (select count(*) from prospects where campaign_id=$1 and state = 'qualified') qualified,
                  (select count(*) from prospects where campaign_id=$1 and state <> 'rejected' and review_reason is not null) for_review,
                  (select count(*) from generated_sites where campaign_id=$1) sites,
                  (select count(*) from generated_sites where campaign_id=$1 and state in ('passed','deployed')) sites_passed,
                  (select count(*) from generated_sites where campaign_id=$1 and state in ('failed','requires_human_review')) sites_failed,
                  (select count(distinct v.site_id) from deployments d join site_versions v on v.id=d.site_version_id
                     join generated_sites g on g.id=v.site_id where g.campaign_id=$1 and d.status='verified') deployed""", campaign_id)
    k = dict(n)
    stage, state = c["current_stage"] or "", c["state"]
    source = {"serp_api": "SerpApi", "google_places": "Google Places", "manual_import": "your imported list"}.get(c["discovery_provider"], "")
    review = f" {_n(k['for_review'], 'business', 'businesses')} kept for your review." if k["for_review"] else ""
    if state == "blocked":
        say = f"{c['name']} is blocked: {c['blocked_reason']}"
        if k["prospects"]:
            say += f" So far: {_n(k['eligible'], 'eligible business', 'eligible businesses')}, {k['qualified']} qualified."
    elif state in ("cancelled", "failed"):
        say = f"{c['name']} {state}." + (f" {c['blocked_reason']}" if c["blocked_reason"] else "")
    elif state == "completed":
        if c["sites_to_build"]:
            say = (f"Done. {_n(k['eligible'], 'business', 'businesses')} verified, {k['qualified']} qualified, and "
                   f"{_n(k['deployed'], 'preview website is', 'preview websites are')} ready.")
            if k["sites_failed"]:
                say += f" {k['sites_failed']} didn't pass QA within the retry budget."
        else:
            say = f"Done. {_n(k['eligible'], 'business', 'businesses')} verified, {k['qualified']} qualified."
        say += review
    elif state == "authorized" or stage in ("queued", ""):
        say = "It's queued; the worker will start it in a moment."
    elif stage == "discovery" and not k["prospects"]:
        say = f"I'm searching {source} for {c['target_count']} {c['niche']} businesses now."
    elif stage == "discovery":
        say = (f"Searching {source}: {_n(k['prospects'], 'candidate')} found, {k['verified']} with the website checked, "
               f"{k['eligible']} of {c['target_count']} kept so far.")
    elif stage == "research":
        say = f"Found {_n(k['eligible'], 'candidate business', 'candidate businesses')}. Researching them now: {k['researched']} done."
    elif stage == "qualification":
        say = f"Researched {k['researched']}. Scoring them now."
    elif stage == "build":
        say = (f"{k['qualified']} qualified. Building {_n(k['sites'], 'preview website')}: "
               f"{k['sites_passed']} passed browser QA so far.")
    elif stage == "deploy":
        say = f"{_n(k['sites_passed'], 'preview')} passed QA. Deploying private previews: {k['deployed']} ready."
    elif stage == "outreach_draft":
        say = f"{_n(k['deployed'], 'preview is', 'previews are')} ready. Drafting outreach for your review (nothing is sent)."
    else:
        say = f"{c['name']} is {state.replace('_', ' ')}, at the {stage.replace('_', ' ')} stage."
    return {"found": True, "campaign_id": str(c["id"]), "state": state, "stage": c["current_stage"],
            "blocked_reason": c["blocked_reason"], "counts": k, "say": say}


async def post_progress(db, campaign_id: UUID) -> str | None:
    """Adds a progress message to the conversation of the operator who approved the campaign, when the work changed in a
    way worth telling (new stage or state, a preview passed QA, a preview was deployed). Returns the message, if any."""
    async with db.transaction() as conn:
        c = await conn.fetchrow("select workspace_id, authorized_by, objective from campaigns where id=$1", campaign_id)
        if c is None or c["authorized_by"] is None:
            return None
        await conn.execute("select pg_advisory_xact_lock(hashtext($1))", f"progress:{campaign_id}")
        p = await campaign_progress(conn, c["workspace_id"], campaign_id)
        k = p["counts"]
        key = f"{p['state']}|{p['stage']}|{k['sites_passed']}|{k['deployed']}"
        last = await conn.fetchval(
            """select meta->>'progress_key' from conversation_messages where workspace_id=$1 and user_id=$2
               and meta->>'kind'='progress' and meta->>'campaign_id'=$3 order by id desc limit 1""",
            c["workspace_id"], c["authorized_by"], str(campaign_id))
        if last == key:
            return None
        channel = (c["objective"] or {}).get("approved_in")
        await conn.execute(
            """insert into conversation_messages(workspace_id, user_id, role, channel, content, meta)
               values ($1,$2,'assistant',$3,$4,$5)""", c["workspace_id"], c["authorized_by"],
            channel if channel in ("text", "voice") else "text", p["say"][:8000],
            {"kind": "progress", "campaign_id": str(campaign_id), "progress_key": key, "agent": "jarvis_progress"})
        return p["say"]

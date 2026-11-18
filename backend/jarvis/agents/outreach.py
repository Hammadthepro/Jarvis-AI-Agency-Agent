"""Agent 10: Outreach.

* Drafts: always available (no email configuration needed). Sonnet writes from VERIFIED observations
  only; each observation cites a fact or verification record.
* Sending: only via the transactional outbox, and only when (a) the workspace's sender readiness
  passes, (b) the campaign has an active outreach authorization, (c) the recipient is a verified,
  public business contact whose source does not prohibit harvesting, (d) suppression is re-checked
  in the same transaction immediately before sending, (e) the preview deployment is verified.
* Ambiguous provider result => UNKNOWN_PROVIDER_RESULT. Never blindly retried."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from uuid import UUID

from jarvis.agents.deps import Deps
from jarvis.core.errors import ComplianceBlocked, ProviderError, ValidationFailed
from jarvis.db.audit import audit
from jarvis.integrations.base import CallContext

DRAFT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["subject", "body", "observation_ids"],
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"},
                               "observation_ids": {"type": "array", "items": {"type": "string"}}}}
DRAFT_SYSTEM = """You write a short, honest, personalised first-contact email from a small website-development agency
to a local US business owner. Use ONLY the observations provided; reference at least one of them specifically and cite
their ids in observation_ids. No flattery you cannot support, no false urgency, no claims about results, no prices,
no "Re:"/"Fwd:" subject prefixes. Mention the concept preview link exactly as given. Under 140 words. Plain text.
Do NOT add an unsubscribe line or postal address; the system appends the compliance footer. Prompt version: outreach-v1."""
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "proton.me", "protonmail.com"}
FORBIDDEN_SENDER_SUFFIXES = (".pages.dev", ".workers.dev")
PO_PATTERNS = ("n/a", "123 main st", "none", "tbd", "example")


def value_hash(kind: str, value: str) -> str:
    v = value.strip().lower()
    if kind == "phone":
        v = "".join(ch for ch in v if ch.isdigit())[-10:]
    return hashlib.sha256(f"{kind}:{v}".encode()).hexdigest()


async def observations(db, prospect_id: UUID) -> list[dict]:
    obs = []
    wv = await db.fetchrow("""select id, status, site_health, reason from website_verifications where prospect_id = $1
                              order by checked_at desc limit 1""", prospect_id)
    if wv and wv["status"] == "VERIFIED_NO_WEBSITE":
        obs.append({"id": f"wv:{wv['id']}", "text": "Our independent checks did not find a website for the business."})
    elif wv and wv["status"] == "VERIFIED_HAS_WEBSITE" and wv["site_health"] in ("broken", "outdated"):
        obs.append({"id": f"wv:{wv['id']}", "text": f"The business's current website appears {wv['site_health']} ({wv['reason']})."})
    for f in await db.fetch("""select id, fact_key, value #>> '{}' v from research_facts where prospect_id = $1 and status='verified'
                               and fact_key in ('service','service_area','specialty','tagline') limit 6""", prospect_id):
        obs.append({"id": f"fact:{f['id']}", "text": f"{f['fact_key']}: {f['v']}"})
    return obs


async def create_draft(deps: Deps, ctx: CallContext, campaign_id: UUID, prospect_id: UUID) -> dict:
    db = deps.db
    existing = await db.fetchrow("select id, state from outreach_drafts where campaign_id=$1 and prospect_id=$2", campaign_id, prospect_id)
    if existing:
        return {"draft_id": str(existing["id"]), "state": existing["state"], "existing": True}
    dep = await db.fetchrow(
        """select d.id, d.url from deployments d join site_versions v on v.id = d.site_version_id
           join generated_sites g on g.id = v.site_id where g.prospect_id = $1 and d.status = 'verified'
           order by d.created_at desc limit 1""", prospect_id)
    obs = await observations(db, prospect_id)
    if not obs:
        raise ValidationFailed("no verified observations to personalise from; draft not created")
    name = await db.fetchval("""select value #>> '{}' from research_facts where prospect_id=$1 and fact_key='business_name'
                                and status='verified' limit 1""", prospect_id)
    user = (f"Business: {name or 'the business'}\nPreview link: {dep['url'] if dep else '(no verified preview)'}\nObservations:\n"
            + "\n".join(f"- id={o['id']}: {o['text']}" for o in obs))
    res = await deps.claude.structured(ctx, model=deps.settings.claude_model_build, system=DRAFT_SYSTEM, user=user,
                                       schema=DRAFT_SCHEMA, max_tokens=1200, operation="outreach.draft")
    valid = {o["id"] for o in obs}
    cited = [i for i in res.data["observation_ids"] if i in valid]
    if not cited:
        raise ValidationFailed("draft did not cite a verified observation; rejected")
    subject = res.data["subject"].strip()
    if subject.lower().startswith(("re:", "fwd:", "fw:")):
        raise ValidationFailed("deceptive subject prefix rejected")
    contact = await db.fetchval(
        """select id from contacts where prospect_id=$1 and contact_type='email' and verification='verified'
           and is_public_business_contact and not harvesting_prohibited order by created_at limit 1""", prospect_id)
    did = await db.fetchval(
        """insert into outreach_drafts(workspace_id, campaign_id, prospect_id, contact_id, deployment_id, subject, body_text,
                                       observations, model) values ($1,$2,$3,$4,$5,$6,$7,$8,$9) returning id""",
        ctx.workspace_id, campaign_id, prospect_id, contact, dep["id"] if dep else None, subject[:200],
        res.data["body"][:4000], [o for o in obs if o["id"] in cited], res.model)
    return {"draft_id": str(did), "state": "draft", "contact": bool(contact), "preview": bool(dep)}


async def readiness(deps: Deps, workspace_id: UUID) -> dict:
    db = deps.db
    r = await db.fetchrow("select * from sender_readiness where workspace_id = $1", workspace_id)
    st = await db.fetchrow("select terms_of_use_url, privacy_policy_url, agency_name from system_settings where workspace_id=$1", workspace_id)
    items = []

    def item(key: str, ok: bool, detail: str) -> None:
        items.append({"key": key, "ok": bool(ok), "detail": detail})

    item("email_provider_configured", deps.email.configured, "EMAIL_PROVIDER=resend and RESEND_API_KEY")
    addr = (r and r["postal_address"]) or ""
    addr_ok = bool(r and addr and r["postal_address_type"] and r["postal_address_attested_at"]
                   and not any(p in addr.lower() for p in PO_PATTERNS) and any(ch.isdigit() for ch in addr)
                   and time.time() - r["postal_address_attested_at"].timestamp() < 365 * 86400)
    item("postal_address", addr_ok, "Legitimate street address, USPS-registered PO Box, or CMRA mailbox, attested within 12 months")
    dom = (r and r["sending_domain"]) or ""
    dom_ok = bool(dom) and not dom.endswith(FORBIDDEN_SENDER_SUFFIXES) and dom not in FREE_MAIL
    item("sending_domain", dom_ok, "Own domain (not pages.dev/workers.dev/free mail)")
    fresh = bool(r and r["dns_checked_at"] and time.time() - r["dns_checked_at"].timestamp() < 86400)
    item("domain_authentication", bool(r and r["spf_ok"] and r["dkim_ok"] and r["dmarc_ok"] and fresh), "SPF, DKIM, DMARC pass (checked within 24h)")
    item("provider_domain_verified", bool(r and r["provider_domain_verified"]), "Email provider reports the domain verified")
    item("from_on_domain", bool(r and r["from_address"] and dom and r["from_address"].lower().endswith("@" + dom.lower())),
         "From address on the sending domain")
    item("reply_to_confirmed", bool(r and r["reply_to_confirmed"]), "Reply-To mailbox confirmed")
    item("unsubscribe_endpoint", bool(r and r["unsubscribe_endpoint_ok"]) and deps.settings.unsubscribe_signing_key is not None,
         "Public HTTPS unsubscribe endpoint tested; UNSUBSCRIBE_SIGNING_KEY set")
    item("terms_and_privacy", bool(st and st["terms_of_use_url"] and st["privacy_policy_url"]), "Public Terms of Use and Privacy Policy URLs")
    item("agency_identity", bool(st and st["agency_name"]), "Agency name (sender identity)")
    return {"ready": all(i["ok"] for i in items), "items": items}


def unsubscribe_token(key: str, workspace_id: UUID, email: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    payload = f"{workspace_id}|{email.lower()}|{ts}"
    sig = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")


def parse_unsubscribe_token(key: str, token: str, max_age_s: int = 3 * 365 * 86400) -> tuple[UUID, str]:
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    ws, email, ts, sig = raw.rsplit("|", 3)
    exp = hmac.new(key.encode(), f"{ws}|{email}|{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(exp, sig) or time.time() - int(ts) > max_age_s:
        raise ValidationFailed("invalid or expired unsubscribe token")
    return UUID(ws), email


async def suppress(conn, workspace_id: UUID, kind: str, value: str, reason: str, source: str) -> None:
    await conn.execute("""insert into suppression_records(workspace_id, kind, value_hash, reason, source) values ($1,$2,$3,$4,$5)
                          on conflict (workspace_id, kind, value_hash) do nothing""", workspace_id, kind, value_hash(kind, value), reason, source)


async def send_draft(deps: Deps, ctx: CallContext, draft_id: UUID, actor: str) -> dict:
    db, s = deps.db, deps.settings
    ready = await readiness(deps, ctx.workspace_id)
    if not ready["ready"]:
        raise ComplianceBlocked("sender readiness incomplete", details={"failing": [i["key"] for i in ready["items"] if not i["ok"]]})
    async with db.transaction() as conn:
        d = await conn.fetchrow("""select d.*, c.value email, c.harvesting_prohibited, c.is_public_business_contact, dep.status dep_status,
                                          dep.url dep_url, p.place_id
                                   from outreach_drafts d left join contacts c on c.id = d.contact_id
                                   left join deployments dep on dep.id = d.deployment_id join prospects p on p.id = d.prospect_id
                                   where d.id = $1 and d.workspace_id = $2 for update of d""", draft_id, ctx.workspace_id)
        if not d:
            raise ValidationFailed("draft not found")
        if d["state"] not in ("authorized", "queued"):
            raise ComplianceBlocked(f"draft state {d['state']} is not authorized for sending")
        auth = await conn.fetchrow("""select * from outreach_authorizations where campaign_id=$1 and revoked_at is null
                                      order by created_at desc limit 1""", d["campaign_id"])
        if not auth:
            raise ComplianceBlocked("no active outreach authorization for this campaign")
        sent_today = await conn.fetchval("""select count(*) from email_outbox o join outreach_drafts d2 on d2.id = o.draft_id
                                            where d2.campaign_id = $1 and o.status in ('sent','sending','unknown_provider_result')
                                            and o.created_at >= date_trunc('day', now())""", d["campaign_id"])
        sent_total = await conn.fetchval("""select count(*) from email_outbox o join outreach_drafts d2 on d2.id = o.draft_id
                                            where d2.campaign_id = $1 and o.status in ('sent','sending','unknown_provider_result')""",
                                         d["campaign_id"])
        if sent_today >= auth["daily_limit"] or sent_total >= auth["total_limit"]:
            raise ComplianceBlocked("authorized send limit reached")
        if not d["email"] or d["harvesting_prohibited"] or not d["is_public_business_contact"]:
            raise ComplianceBlocked("recipient is not a verified public business contact that permits collection")
        if d["dep_status"] != "verified":
            raise ComplianceBlocked("preview deployment is not verified")
        domain = d["email"].split("@")[1]
        hashes = [("email", value_hash("email", d["email"])), ("domain", value_hash("domain", domain))]
        if d["place_id"]:
            hashes.append(("place_id", value_hash("place_id", d["place_id"])))
        supp = await conn.fetchval("""select count(*) from suppression_records where workspace_id=$1 and
                                      (kind, value_hash) in (select * from unnest($2::text[], $3::text[]))""",
                                   ctx.workspace_id, [h[0] for h in hashes], [h[1] for h in hashes])
        if supp:
            await conn.execute("update outreach_drafts set state='suppressed' where id=$1", draft_id)
            return {"status": "suppressed"}
        key = f"outreach:{draft_id}"
        row = await conn.fetchrow("""insert into email_outbox(workspace_id, draft_id, idempotency_key, status) values ($1,$2,$3,'sending')
                                     on conflict (draft_id) do nothing returning id""", ctx.workspace_id, draft_id, key)
        if row is None:
            ob = await conn.fetchrow("select status from email_outbox where draft_id=$1", draft_id)
            return {"status": ob["status"], "note": "already in outbox; not resent"}
        await conn.execute("update outreach_drafts set state='sending' where id=$1", draft_id)
        await audit(conn, workspace_id=ctx.workspace_id, actor_type="user", actor_id=actor, action="outreach.send_started",
                    entity_type="outreach_draft", entity_id=draft_id, data={"authorization_id": str(auth["id"])})
    r = await db.fetchrow("select * from sender_readiness where workspace_id=$1", ctx.workspace_id)
    assert s.unsubscribe_signing_key
    tok = unsubscribe_token(s.unsubscribe_signing_key.get_secret_value(), ctx.workspace_id, d["email"])
    unsub = f"{s.api_public_base_url}/u/{tok}"
    agency = await db.fetchval("select agency_name from system_settings where workspace_id=$1", ctx.workspace_id)
    footer = (f"\n\n--\nThis is a commercial message from {agency}.\n{r['postal_address']}\n"
              f"To stop receiving emails from us: {unsub} (or reply STOP).")
    try:
        result = await deps.email.send(ctx, idempotency_key=key, from_addr=f"{agency} <{r['from_address']}>", to=d["email"],
                                       reply_to=r["reply_to"], subject=d["subject"], text=d["body_text"] + footer,
                                       headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    except ProviderError as exc:
        status = "unknown_provider_result" if exc.billing_ambiguous else "failed"
        async with db.transaction() as conn:
            await conn.execute("update email_outbox set status=$2, last_error=$3, attempts=attempts+1 where draft_id=$1",
                               draft_id, status, exc.message[:500])
            await conn.execute("update outreach_drafts set state=$2 where id=$1", draft_id, status)
        return {"status": status, "error": exc.message[:200]}
    async with db.transaction() as conn:
        await conn.execute("update email_outbox set status='sent', provider_message_id=$2, attempts=attempts+1 where draft_id=$1",
                           draft_id, result.message_id)
        await conn.execute("update outreach_drafts set state='sent' where id=$1", draft_id)
        await audit(conn, workspace_id=ctx.workspace_id, actor_type="user", actor_id=actor, action="outreach.sent",
                    entity_type="outreach_draft", entity_id=draft_id, data={"provider_message_id": result.message_id})
    return {"status": "sent", "provider_message_id": result.message_id}

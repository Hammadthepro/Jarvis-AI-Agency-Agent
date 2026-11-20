"""One conversational front door for JARVIS, shared by voice and text.

A turn of natural language is routed onto the SAME command handlers the text console uses (create, answer,
confirm, cancel) and the same campaign records; there is no voice-specific business logic. Routing:
  1. "confirm / go ahead"           -> confirm the user's latest command awaiting confirmation
  2. "cancel / never mind"          -> reject the user's latest pending command
  3. "status / how is it going"     -> report real campaign progress from the database
  4. a pending clarification exists -> treat the utterance as the answer to its first question
  5. otherwise                      -> a new command
Replies are short, speakable sentences describing what actually happened; nothing is claimed that the
database does not show."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from jarvis.agents import command as cmd
from jarvis.api.auth import CurrentPrincipal, Forbidden, Principal, Unauthorized
from jarvis.api.routes.campaigns import AnswersIn, CommandIn, ConfirmIn, answer, confirm, create_command
from jarvis.core.errors import ComplianceBlocked, JarvisError

router = APIRouter()
log = logging.getLogger("jarvis.conversation")
TURN_ID = r"^[A-Za-z0-9._:-]{1,80}$"

CONFIRM = re.compile(r"^(yes,? )?(confirm(ed)?|go ahead|do it|authori[sz]e( it)?|start( it)?|proceed|yes)( please)?$")
CANCEL = re.compile(r"^(cancel( that| it)?|never ?mind|forget (it|that)|stop that|no,? cancel( it)?)( please)?$")
STATUS = re.compile(r"\b(status|progress|how('s| is) it going|what are you (doing|working on)|any updates?|where are we|"
                    r"what('s| is) happening)\b")
NUM = {**cmd._NUM_WORDS, "six": 6, "seven": 7, "eight": 8, "nine": 9, "fifteen": 15, "thirty": 30, "forty": 40}
STATE_NAMES = {"texas": "TX", "california": "CA", "florida": "FL", "new york": "NY", "georgia": "GA", "arizona": "AZ",
               "illinois": "IL", "ohio": "OH", "pennsylvania": "PA", "north carolina": "NC", "washington": "WA",
               "colorado": "CO", "tennessee": "TN", "nevada": "NV", "oregon": "OR", "michigan": "MI", "virginia": "VA",
               "massachusetts": "MA", "new jersey": "NJ", "louisiana": "LA", "oklahoma": "OK", "utah": "UT"}
PENDING_WINDOW = "30 minutes"


def _number(text: str) -> int | None:
    m = re.search(r"\b(\d{1,4})\b", text)
    if m:
        return int(m.group(1))
    return next((v for w, v in NUM.items() if re.search(rf"\b{w}\b", text)), None)


def answer_from_speech(question: dict, text: str):
    """Maps a spoken answer onto the structured answer the command pipeline expects; None if unclear."""
    field, low, opts = question.get("field", ""), text.lower().strip(" .!?"), question.get("options") or []
    if field == "missing_rating_policy":
        if re.search(r"\b(exclude|skip|drop|remove|leave (them )?out|no)\b", low):
            return "exclude"
        if re.search(r"\b(include|keep|review|yes)\b", low):
            return "include_for_review"
        return None
    if field == "minimum_google_rating" or (opts and all(isinstance(o, (int, float)) for o in opts)):
        m = re.search(r"(\d(?:\.\d)?)", low.replace(" point ", "."))
        try:
            v = Decimal(m.group(1)) if m else None
        except InvalidOperation:
            v = None
        if v is None:
            return None
        if opts and float(v) not in [float(o) for o in opts]:
            return None
        return str(v)
    if field == "target_count":
        n = _number(low)
        return n if n and 1 <= n <= 1000 else None
    if field == "location.state":
        up = low.upper()
        if up in cmd.US_STATES:
            return up
        return next((ab for name, ab in STATE_NAMES.items() if name in low), None)
    if field == "location":
        r = cmd.deterministic_parse(f"find 1 x in {low}")
        return r.draft.get("location")
    if field == "niche":
        return low or None
    if opts:
        return next((o for o in opts if str(o).lower() in low), None)
    return None


async def campaign_progress(db, workspace_id, campaign_id) -> dict:
    c = await db.fetchrow("""select id, name, state, current_stage, blocked_reason, sites_to_build from campaigns
                             where id=$1 and workspace_id=$2""", campaign_id, workspace_id)
    if c is None:
        return {"found": False, "say": "I can't find that campaign."}
    n = await db.fetchrow(
        """select (select count(*) from prospects where campaign_id=$1) prospects,
                  (select count(*) from prospects where campaign_id=$1 and state not in ('rejected')) eligible,
                  (select count(*) from generated_sites where campaign_id=$1) sites,
                  (select count(*) from generated_sites where campaign_id=$1 and state in ('passed','deployed')) sites_passed,
                  (select count(distinct v.site_id) from deployments d join site_versions v on v.id=d.site_version_id
                     join generated_sites g on g.id=v.site_id where g.campaign_id=$1 and d.status='verified') deployed""", campaign_id)
    say = f"{c['name']} is {c['state'].replace('_', ' ')}"
    if c["current_stage"] and c["state"] not in ("completed",):
        say += f", at the {c['current_stage'].replace('_', ' ')} stage"
    say += f". {n['prospects']} prospects so far"
    if c["sites_to_build"]:
        say += f", {n['sites']} previews built, {n['sites_passed']} passed QA, {n['deployed']} deployed and verified"
    say += "."
    if c["blocked_reason"]:
        say += f" It is blocked: {c['blocked_reason']}"
    return {"found": True, "campaign_id": str(c["id"]), "state": c["state"], "stage": c["current_stage"],
            "blocked_reason": c["blocked_reason"], "counts": dict(n), "say": say}


async def _status(db, me: Principal) -> dict:
    rows = await db.fetch("""select id from campaigns where workspace_id=$1 and state in ('authorized','running','blocked','paused')
                             order by created_at desc limit 3""", me.workspace_id)
    pending = await db.fetchval("""select count(*) from commands where workspace_id=$1 and user_id=$2
                                   and status in ('needs_clarification','awaiting_confirmation')""", me.workspace_id, me.user_id)
    if not rows:
        last = await db.fetchrow("select id from campaigns where workspace_id=$1 order by created_at desc limit 1", me.workspace_id)
        say = "Nothing is running right now."
        if last:
            say += " The most recent campaign: " + (await campaign_progress(db, me.workspace_id, last["id"]))["say"]
    else:
        say = " ".join([(await campaign_progress(db, me.workspace_id, r["id"]))["say"] for r in rows])
    if pending:
        say += f" You have {pending} command{'s' if pending > 1 else ''} waiting for your answer or confirmation."
    return {"kind": "status", "say": say}


async def deterministic_turn(request: Request, me: Principal, text: str, channel: str) -> dict:
    db = request.app.state.db
    low = " ".join(text.lower().strip().rstrip(".!?").split())
    pending = await db.fetchrow(
        f"""select id, status, questions from commands where workspace_id=$1 and user_id=$2
              and status in ('needs_clarification','awaiting_confirmation') and created_at > now() - interval '{PENDING_WINDOW}'
            order by created_at desc limit 1""", me.workspace_id, me.user_id)
    try:
        if CONFIRM.match(low):
            if not pending or pending["status"] != "awaiting_confirmation":
                return {"kind": "confirm", "say": "There is nothing waiting for confirmation."}
            out = await confirm(pending["id"], ConfirmIn(), request, me)
            prog = await campaign_progress(db, me.workspace_id, UUID(str(out["campaign_id"])))
            return {"kind": "confirmed", "campaign_id": str(out["campaign_id"]),
                    "say": "Campaign authorized and queued. " + prog["say"] + " I'll keep you posted."}
        if CANCEL.match(low):
            if not pending:
                return {"kind": "cancel", "say": "There is no pending command to cancel."}
            await db.execute("update commands set status='rejected', error='cancelled by user' where id=$1", pending["id"])
            return {"kind": "cancelled", "command_id": str(pending["id"]), "say": "Cancelled. Nothing was started."}
        if STATUS.search(low) and len(low.split()) <= 12:
            return await _status(db, me)
        if pending and pending["status"] == "needs_clarification" and pending["questions"]:
            q = pending["questions"][0]
            val = answer_from_speech(q, text)
            if val is None:
                return {"kind": "clarify", "command_id": str(pending["id"]), "say": f"Sorry, I didn't get that. {q['prompt']}"}
            res = await answer(pending["id"], AnswersIn(answers={q["field"]: val}), request, me)
            return _describe(res)
        res = await create_command(CommandIn(text=text, channel=channel), request, me)
        return _describe(res)
    except ComplianceBlocked as exc:
        return {"kind": "blocked", "say": f"I can't start that: {exc.message}. Import a business list you have the right to use "
                                          "from Campaigns, and I can work on that instead."}
    except JarvisError as exc:
        return {"kind": "error", "say": f"That didn't work: {exc.message}", "error": {"code": exc.code, "message": exc.message}}


def _describe(res: dict) -> dict:
    base = {"command_id": str(res["id"]), "status": res["status"]}
    if res["status"] == "needs_clarification":
        return {**base, "kind": "clarify", "say": res["questions"][0]["prompt"]}
    if res["status"] != "awaiting_confirmation":
        return {**base, "kind": "error", "say": f"I couldn't use that request ({res.get('error') or res['status']})."}
    p = res.get("parsed") or {}
    loc = ", ".join(x for x in ((p.get("location") or {}).get("city"), (p.get("location") or {}).get("state")) if x)
    say = f"I'll look for {p.get('target_count')} {p.get('niche')} businesses in {loc}"
    wf = [w for w in p.get("website_filter") or [] if w != "ANY"]
    if wf:
        say += " with " + " or ".join(w.replace("WEBSITE_WORKING_OUTDATED", "outdated websites").replace("NO_WEBSITE", "no website")
                                      .replace("WEBSITE_BROKEN", "broken websites") for w in wf)
    if p.get("minimum_google_rating"):
        say += f", rated at least {p['minimum_google_rating']}"
    if p.get("sites_to_build"):
        say += f", then build, test and deploy {p['sites_to_build']} preview website{'s' if p['sites_to_build'] != 1 else ''}"
    plan = res.get("plan") or {}
    if plan.get("estimated_usd_max_if_paid") is not None:
        say += f". Worst-case provider cost if billed: about {plan['estimated_usd_max_if_paid']} dollars"
    if res.get("compliance") and not res["compliance"].get("open"):
        return {**base, "kind": "blocked", "say": say + ". But Google Places discovery is blocked until written approval is "
                "recorded, so I can't start it. Import a business list from Campaigns and I'll run the rest of the workflow on it."}
    return {**base, "kind": "confirm_needed", "say": say + ". Say confirm to start, or cancel."}


# ------------------------------------------------------------------ the shared brain (voice and text)
async def _store(db, me: Principal, role: str, channel: str, content: str, meta: dict | None = None) -> None:
    await db.execute("""insert into conversation_messages(workspace_id, user_id, role, channel, content, meta)
                        values ($1,$2,$3,$4,$5,$6)""", me.workspace_id, me.user_id, role, channel, content[:8000], meta or {})


async def handle_turn(request: Request, me: Principal, text: str, channel: str, turn_id: str | None = None,
                      voice: dict | None = None) -> dict:
    """One turn of the single JARVIS conversation (voice and text alike).

    Mode "auto"/"claude": Claude with tools. If Claude is not configured or the call fails, the reply says so plainly
    (`kind: provider_error`, with the sanitized provider reason) and NOTHING else happens: there is no silent switch to
    another agent. Mode "deterministic" (explicit setting) uses the basic command parser, labelled as such."""
    from jarvis.api.conversation_agent import claude_available, claude_turn
    from jarvis.core.errors import BudgetBlocked, ProviderError
    from jarvis.core.integrations import DISABLED
    db = request.app.state.db
    s = request.app.state.deps.settings
    t0 = time.perf_counter()
    if turn_id:
        # A voice turn is identified by the agent's correlation id: a retried request returns the stored reply instead of
        # running the turn (and any tool) a second time.
        prev = await db.fetchrow("""select content, meta from conversation_messages where workspace_id=$1 and user_id=$2
                                   and role='assistant' and meta->>'turn_id'=$3 order by id desc limit 1""",
                                 me.workspace_id, me.user_id, turn_id)
        if prev is not None:
            log.info("conversation.turn_replayed turn=%s", turn_id)
            m = dict(prev["meta"] or {})
            return {**{k: m.get(k) for k in ("kind", "command_id", "campaign_id", "agent", "provider_error") if m.get(k)},
                    "say": prev["content"], "turn_id": turn_id, "replayed": True}
    await _store(db, me, "user", channel, text, {"turn_id": turn_id} if turn_id else None)
    log.info("conversation.turn_received turn=%s channel=%s chars=%d", turn_id, channel, len(text))
    if s.jarvis_conversation_agent == "deterministic":
        out = await deterministic_turn(request, me, text, channel)
        out["agent"] = "basic_parser"
    elif not claude_available(request):
        why = "Anthropic is disabled in Integrations" if "anthropic" in DISABLED else \
            "Anthropic is not configured (Integrations > Anthropic)"
        out = _provider_failure(f"Claude unavailable: {why}.", {"state": "DISABLED" if "anthropic" in DISABLED else "NOT_CONFIGURED"})
    else:
        log.info("conversation.claude_request_start turn=%s", turn_id)
        try:
            out = await claude_turn(request, me, text, channel, voice)
        except BudgetBlocked as exc:
            out = _provider_failure(f"Claude unavailable: the Anthropic budget limit was reached ({exc.message}).",
                                    {"state": "BUDGET_BLOCKED"})
        except ProviderError as exc:
            out = _provider_failure(f"Claude unavailable: {getattr(exc, 'reason', exc.message)}",
                                    {"state": exc.state.value, **{k: v for k, v in (exc.details or {}).items()
                                                                  if k in ("exception", "http_status", "error_type", "request_id")}})
    meta = {k: out.get(k) for k in ("kind", "command_id", "campaign_id", "agent", "provider_error") if out.get(k)}
    meta.update({k: v for k, v in (out.get("meta") or {}).items() if k in ("tools", "agent")})
    if turn_id:
        meta["turn_id"] = turn_id
    await _store(db, me, "assistant", channel, out.get("say") or "", meta)
    out.pop("meta", None)
    log.info("conversation.reply turn=%s kind=%s agent=%s ms=%d", turn_id, out.get("kind"), out.get("agent"),
             int((time.perf_counter() - t0) * 1000))
    if turn_id:
        out["turn_id"] = turn_id
    return out


def _provider_failure(reason: str, info: dict) -> dict:
    return {"kind": "provider_error", "agent": "claude", "provider_error": info,
            "say": f"{reason} I haven't done anything with that request. Ask again once it's fixed."}


@router.get("/conversation/messages")
async def conversation_messages(request: Request, me: Principal = CurrentPrincipal, after: int = 0, limit: int = 60):
    rows = await request.app.state.db.fetch(
        """select id, role, channel, content, meta, created_at from conversation_messages where workspace_id=$1 and user_id=$2
           and id > $3 order by id desc limit $4""", me.workspace_id, me.user_id, after, min(max(limit, 1), 200))
    import json as _json
    return _json.loads(_json.dumps([dict(r) for r in reversed(rows)], default=str))


# ------------------------------------------------------------------ text entry point (same router)
class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/conversation/turn")
async def conversation_turn(body: TurnIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    return await handle_turn(request, me, body.text, "text")


@router.post("/commands/{command_id}/cancel")
async def cancel_command(command_id: UUID, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    st = await request.app.state.db.fetchval(
        """update commands set status='rejected', error='cancelled by user' where id=$1 and workspace_id=$2
           and status in ('received','needs_clarification','awaiting_confirmation') returning status""", command_id, me.workspace_id)
    if st is None:
        from jarvis.core.errors import InvalidTransition
        raise InvalidTransition("command is not pending")
    return {"ok": True}


# ------------------------------------------------------------------ voice agent (internal, shared secret)
class VoiceIn(BaseModel):
    user_id: UUID
    workspace_id: UUID
    text: str = Field(min_length=1, max_length=2000)
    turn_id: str | None = Field(default=None, pattern=TURN_ID)   # the agent's correlation id for this spoken turn
    # When the owner talked over JARVIS's previous reply: the part of it they actually heard (from the agent's playout).
    interrupted_heard: str | None = Field(default=None, max_length=2000)


def _check_agent(request: Request, key: str | None) -> None:
    s = request.app.state.settings
    if not s.voice_agent_shared_secret or not key or not hmac.compare_digest(key, s.voice_agent_shared_secret.get_secret_value()):
        raise Unauthorized("voice agent key invalid or voice NOT_CONFIGURED")


async def _member(request: Request, user_id: UUID, workspace_id: UUID) -> Principal:
    role = await request.app.state.db.fetchval("select role from memberships where workspace_id=$1 and user_id=$2", workspace_id, user_id)
    if role is None:
        raise Forbidden("not a member of this workspace")
    return Principal(user_id, None, workspace_id, role)


@router.post("/internal/voice/command")
async def voice_command(body: VoiceIn, request: Request, x_voice_agent_key: str | None = Header(None)):
    """Called only by the JARVIS voice agent, for the participant the API minted the room token for."""
    _check_agent(request, x_voice_agent_key)
    me = await _member(request, body.user_id, body.workspace_id)
    if me.role == "viewer":
        return {"kind": "error", "say": "Your role is viewer, so I can report status but not start work."} \
            if not STATUS.search(body.text.lower()) else await _status(request.app.state.db, me)
    voice = {"previous_reply_interrupted": True, "owner_heard_only": body.interrupted_heard} \
        if body.interrupted_heard is not None else None
    return await handle_turn(request, me, body.text.strip(), "voice", body.turn_id, voice)


@router.get("/internal/voice/progress")
async def voice_progress(campaign_id: UUID, user_id: UUID, workspace_id: UUID, request: Request,
                         x_voice_agent_key: str | None = Header(None)):
    _check_agent(request, x_voice_agent_key)
    await _member(request, user_id, workspace_id)
    return await campaign_progress(request.app.state.db, workspace_id, campaign_id)


@router.post("/internal/voice/heartbeat")
async def voice_heartbeat(body: dict, request: Request, x_voice_agent_key: str | None = Header(None)):
    _check_agent(request, x_voice_agent_key)
    agent_id = str(body.get("agent_id") or "")[:100]
    if not agent_id:
        raise Unauthorized("agent_id required")
    db = request.app.state.db
    if body.get("event") == "process_ready":            # sent from inside a job process after prewarm
        await db.execute("""insert into voice_agent_heartbeats(agent_id, last_seen_at, process_ready_at) values ($1, now(), now())
                            on conflict (agent_id) do update set process_ready_at=now()""", agent_id)
        return {"ok": True}
    await db.execute(
        """insert into voice_agent_heartbeats(agent_id, version, last_seen_at, detail, registered, worker_id, last_error)
           values ($1,$2,now(),$3,$4,$5,$6)
           on conflict (agent_id) do update set last_seen_at=now(), version=excluded.version, detail=excluded.detail,
             registered=excluded.registered, worker_id=excluded.worker_id, last_error=excluded.last_error""",
        agent_id, str(body.get("version") or "")[:40], str(body.get("detail") or "")[:300] or None,
        bool(body.get("registered")), (str(body.get("worker_id"))[:100] if body.get("worker_id") else None),
        (str(body.get("last_error"))[:300] if body.get("last_error") else None))
    return {"ok": True}


@router.get("/internal/voice/config")
async def voice_config(request: Request, x_voice_agent_key: str | None = Header(None)):
    """LiveKit credentials for the voice agent when they were saved in the Integrations Center. Internal network
    only (private or loopback caller) and shared-secret authenticated; never reachable from a browser."""
    _check_agent(request, x_voice_agent_key)
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        internal = ip.is_private or ip.is_loopback
    except ValueError:
        internal = host in ("testclient", "test")
    if not internal:
        raise Forbidden("voice agent configuration is only served on the internal network")
    lk = request.app.state.livekit
    from jarvis.core.integrations import DISABLED
    if "livekit" in DISABLED or not lk.configured:
        return {"state": "DISABLED" if "livekit" in DISABLED else "NOT_CONFIGURED"}
    s = lk.settings
    return {"state": "CONFIGURED", "url": s.livekit_url, "api_key": s.livekit_api_key.get_secret_value(),
            "api_secret": s.livekit_api_secret.get_secret_value()}

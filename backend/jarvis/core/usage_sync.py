"""Scheduled provider-usage reconciliation.

Writes account-level rows to provider_usage_reports (provider accounts belong to the deployment, not a
workspace). Every provider gets a row each run with an explicit status:
  ok             - the provider reported usage (reported_units / reported_cost_micros set)
  unavailable    - no usage API is integrated for this provider; usage is UNKNOWN, not zero
  not_configured - no credential
  error          - the usage API call failed
Missing data is never written as zero.

Cost basis (provider_usage_reports.cost_basis) says what any reported cost is: provider_reported,
provider_invoice, local_estimate or manual_entry. A cost priced by JARVIS is always local_estimate and is never
presented as a provider invoice."""

from __future__ import annotations

from datetime import UTC, datetime

from jarvis.core.errors import ProviderError

UNAVAILABLE = {
    "google_places": "No Google usage API is integrated. Import Cloud Billing reports or enter usage manually (POST /usage/reports).",
    "cloudflare": "Pages Direct Upload has no metered charge on the free plan and no usage API is integrated.",
    "resend": "No Resend usage API is integrated. Check the Resend dashboard.",
    "livekit": "No LiveKit usage API is integrated. Check LiveKit Cloud billing.",
}


async def _write(db, provider: str, unit: str, status: str, *, units=None, cost=None, source="provider_api", raw=None,
                 detail=None, cost_basis=None) -> None:
    if cost is not None and cost_basis is None:
        raise ValueError("a reported cost needs an explicit cost_basis")
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await db.execute(
        """insert into provider_usage_reports(workspace_id, provider, period_start, period_end, unit, reported_units,
               reported_cost_micros, source, raw, status, detail, cost_basis)
           values (null,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
        provider, start, now, unit, units, cost, source, raw, status, detail, cost_basis)


async def sync_provider_usage(deps) -> dict[str, str]:
    db, out = deps.db, {}
    return await _sync(deps, db, out)


async def _sync(deps, db, out: dict[str, str]) -> dict[str, str]:

    # SerpApi: account endpoint (free) reports searches used this month.
    if not deps.serp.configured:
        await _write(db, "serp_api", "search", "not_configured", source="none", detail="SERPAPI_API_KEY not set")
        out["serp_api"] = "not_configured"
    else:
        try:
            acct = await deps.serp.account()
            used = acct.get("this_month_usage")
            if used is None:
                await _write(db, "serp_api", "search", "unavailable", source="provider_api", raw=acct,
                             detail="account response did not include this_month_usage")
                out["serp_api"] = "unavailable"
            else:
                await _write(db, "serp_api", "search", "ok", units=int(used), raw=acct)
                out["serp_api"] = "ok"
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            await _write(db, "serp_api", "search", "error", source="provider_api", detail=str(exc)[:300])
            out["serp_api"] = "error"

    # Firecrawl: credit usage endpoint.
    if not deps.firecrawl.configured:
        await _write(db, "firecrawl", "credit", "not_configured", source="none", detail="FIRECRAWL_API_KEY not set")
        out["firecrawl"] = "not_configured"
    else:
        try:
            data = (await deps.firecrawl.credit_usage()).get("data") or {}
            remaining, plan = data.get("remaining_credits"), data.get("plan_credits")
            if remaining is not None and plan is not None:
                await _write(db, "firecrawl", "credit", "ok", units=int(plan) - int(remaining), raw=data)
                out["firecrawl"] = "ok"
            elif remaining is not None:
                await _write(db, "firecrawl", "credit_remaining", "ok", units=int(remaining), raw=data,
                             detail="provider reports remaining credits only; credits used are not derivable")
                out["firecrawl"] = "ok"
            else:
                await _write(db, "firecrawl", "credit", "unavailable", raw=data, detail="credit usage response had no credit fields")
                out["firecrawl"] = "unavailable"
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            await _write(db, "firecrawl", "credit", "error", detail=str(exc)[:300])
            out["firecrawl"] = "error"

    # Anthropic: no Usage & Cost Admin API is integrated, so ACCOUNT-LEVEL usage and billing are UNAVAILABLE.
    # What JARVIS does know is recorded in `raw`, labelled: token counts the API returned on each of JARVIS's
    # own responses, and a cost JARVIS priced locally from those tokens (local_estimate, never an invoice).
    if not deps.claude.configured:
        await _write(db, "anthropic", "tokens", "not_configured", source="none", detail="ANTHROPIC_API_KEY not set")
        out["anthropic"] = "not_configured"
    else:
        row = await db.fetchrow("""select coalesce(sum(tokens_in + tokens_out), 0)::bigint t, coalesce(sum(estimated_cost_micros), 0)::bigint c,
                                          count(*) n from usage_events where provider = 'anthropic' and outcome = 'ok'
                                          and created_at >= date_trunc('month', now())""")
        await _write(db, "anthropic", "tokens", "unavailable", source="none",
                     raw={"jarvis_responses": row["n"], "jarvis_response_tokens": row["t"],
                          "jarvis_cost_micros": row["c"], "jarvis_cost_basis": "local_estimate"},
                     detail=("Account-level usage and billing UNAVAILABLE: the Anthropic usage/cost API is not integrated. "
                             f"JARVIS's own calls this month: {row['n']} responses, {row['t']} tokens as reported per response; "
                             "their cost is a local estimate from the configured price table, not a provider invoice."))
        out["anthropic"] = "unavailable"

    for provider, why in UNAVAILABLE.items():
        configured = {"google_places": deps.places.configured, "cloudflare": deps.cloudflare.configured,
                      "resend": deps.email.configured, "livekit": bool(deps.settings.livekit_api_key)}[provider]
        await _write(db, provider, "requests", "unavailable" if configured else "not_configured", source="none",
                     detail=why if configured else "credential not set")
        out[provider] = "unavailable" if configured else "not_configured"
    return out

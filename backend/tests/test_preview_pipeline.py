"""The website preview pipeline's honesty and failure paths, through the real worker.

Provider HTTP is served by test doubles (SIMULATED providers); the worker, queue, builder, renderer, browser QA
(local subprocess here; the Docker sandbox in the sandbox test when the image is present) and deployment code are real."""
import subprocess

import pytest

from jarvis.agents import objective as obj
from tests.conftest import login
from tests.doubles import Site
from tests.test_objective_and_serp_discovery import no_website_business, vet
from tests.test_workflow_e2e import run_until

HAVE_SANDBOX = subprocess.run(["docker", "image", "inspect", "jarvis-qa-sandbox:latest"], capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    import jarvis.db.queue as q
    monkeypatch.setattr(q, "backoff_seconds", lambda *a, **k: 0.0)      # same retry LIMITS, no waiting between attempts


async def one_vet_campaign(client, deps, web, email, *, previews=1):
    from uuid import UUID

    from jarvis.api.auth import Principal
    from jarvis.api.routes.campaigns import _create_campaign
    web.dns.add("www.bbb.org")
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")
    no_website_business(web, "Riverbend Animal Hospital", "(615) 555-0121", "Nashville", "TN", "riverbend")
    web.maps["Nashville, Tennessee"] = [vet("Riverbend Animal Hospital", "(615) 555-0121", "4 Broadway, Nashville, TN 37203")]
    a = await login(client, email)
    o = {"task": "find_and_build_previews", "business_category": "veterinary clinic", "area": {"scope": "nationwide"},
         "target_count": 1, "previews_to_build": previews}
    web.maps = {"Nashville, Tennessee": web.maps["Nashville, Tennessee"]}
    me = Principal(UUID(a["me"]["user_id"]), None, UUID(a["me"]["workspace_id"]), "owner")
    # nationwide search, but only Nashville returns listings in this double
    cid = (await _create_campaign(deps, me, obj.resolve(o)["parameters"], command_id=None, budget_cap=None, per_site=None,
                                  objective=o))["campaign_id"]
    return a, me, cid


async def test_site_uses_the_business_location_not_the_campaign_scope(client, deps, web, db, fake_claude):
    a, me, cid = await one_vet_campaign(client, deps, web, "loc@t.dev")
    assert await run_until(deps, cid, timeout=240) == "completed"
    spec_calls = [c for c in fake_claude.calls if "sitespec" in c["system"]]
    assert spec_calls and "Campaign location: Nashville, TN" in spec_calls[0]["messages"][0]["content"]
    sites = (await client.get(f"/sites?campaign_id={cid}", headers=a["headers"])).json()
    assert sites[0]["state"] == "deployed" and sites[0]["preview_url"].endswith(".pages.dev")
    assert sites[0]["deploy_status"] == "verified" and sites[0]["last_error"] is None
    detail = (await client.get(f"/sites/{sites[0]['id']}", headers=a["headers"])).json()
    assert detail["site"]["business_name"] == "Riverbend Animal Hospital"
    shot = await client.get(f"/sites/{sites[0]['id']}/artifact/{detail['versions'][0]['version']}/qa/desktop.png", headers=a["headers"])
    assert shot.status_code == 200 and shot.content[:8] == b"\x89PNG\r\n\x1a\n"                 # real QA screenshot served
    html = (await client.get(f"/sites/{sites[0]['id']}/artifact/{detail['versions'][0]['version']}/dist/index.html",
                             headers=a["headers"])).text
    assert "Riverbend Animal Hospital" in html and "(615) 555-0121" in html                    # the business's own details


async def test_qa_that_cannot_run_is_never_passed_and_the_campaign_is_not_completed(client, deps, web, db):
    deps.settings = deps.settings.model_copy(update={"qa_runner_mode": "docker", "sandbox_image": "jarvis-qa-sandbox-missing:never"})
    a, me, cid = await one_vet_campaign(client, deps, web, "qafail@t.dev")
    final = await run_until(deps, cid, states=("completed", "blocked", "failed", "requires_human_review"), timeout=240)
    assert final == "requires_human_review"                                    # not "completed" with zero previews
    site = await db.fetchrow("select state, last_error from generated_sites where campaign_id=$1::uuid", cid)
    assert site["state"] == "failed" and "QaRunnerError" in site["last_error"] and "attempt 3 of 3, final" in site["last_error"]
    runs = await db.fetch("""select q.status from qa_runs q join site_versions v on v.id=q.site_version_id
                             join generated_sites g on g.id=v.site_id where g.campaign_id=$1::uuid""", cid)
    assert runs and all(r["status"] == "error" for r in runs)                  # recorded as errors, never as passed
    assert await db.fetchval("select count(*) from deployments where workspace_id=$1", me.workspace_id) == 0
    from uuid import UUID

    from jarvis.workflow.progress import campaign_progress
    say = (await campaign_progress(db, me.workspace_id, UUID(cid)))["say"]
    assert "needs your attention" in say and "No preview website was completed" in say and "QaRunnerError" in say


async def test_deployment_that_does_not_verify_is_a_failure_and_is_retried(client, deps, web, db):
    broken = []

    async def runner(directory, branch):                  # upload "succeeds" but the URL never serves the site
        web.deploy_runs.append((str(directory), branch))
        broken.append(branch)
        return 0, f"Deployment complete! https://{branch}.jarvis-previews.pages.dev", ""
    deps.cloudflare.runner = runner
    import jarvis.agents.deployment as dep
    orig = dep.deploy_site

    async def quick(d, ctx, site_id, verify_attempts=2, verify_delay_s=0):
        return await orig(d, ctx, site_id, verify_attempts=verify_attempts, verify_delay_s=verify_delay_s)
    dep.deploy_site = quick
    try:
        a, me, cid = await one_vet_campaign(client, deps, web, "depfail@t.dev")
        final = await run_until(deps, cid, states=("completed", "blocked", "failed", "requires_human_review"), timeout=240)
    finally:
        dep.deploy_site = orig
    assert final == "requires_human_review"
    assert len(broken) == 3                                                     # retried within the 3-attempt limit
    rows = await db.fetch("select status from deployments where workspace_id=$1", me.workspace_id)
    assert rows and all(r["status"] == "failed" for r in rows)                  # never verified
    site = await db.fetchrow("select state, last_error from generated_sites where campaign_id=$1::uuid", cid)
    assert site["state"] == "failed" and "did not verify" in site["last_error"]
    shown = (await client.get(f"/sites?campaign_id={cid}", headers=a["headers"])).json()[0]      # what the dashboard shows
    assert shown["business_name"] == "Riverbend Animal Hospital" and shown["qa_status"] == "passed"
    assert shown["deploy_status"] == "failed" and shown["preview_url"] is None and "did not verify" in shown["last_error"]
    assert shown["last_task"]["task"] == "site.deploy" and shown["last_task"]["attempts"] == 3


def test_preview_branches_are_unique_per_site():
    from jarvis.agents.deployment import preview_branch
    a = preview_branch("pest-control-services-company-1234abcd")
    b = preview_branch("pest-control-services-company-9999ffff")
    assert a != b and a.endswith("-1234abcd") and len(a) <= 28
    assert preview_branch("veterinary-clinic-ab12cd34") == "veterinary-clinic-ab12cd34"


@pytest.mark.skipif(not HAVE_SANDBOX, reason="jarvis-qa-sandbox:latest image not built (scripts/dev-up.sh builds it)")
async def test_generated_site_is_qad_in_the_isolated_docker_sandbox(client, deps, web, db):
    deps.settings = deps.settings.model_copy(update={"qa_runner_mode": "docker", "sandbox_image": "jarvis-qa-sandbox:latest"})
    a, me, cid = await one_vet_campaign(client, deps, web, "sandbox@t.dev")
    assert await run_until(deps, cid, timeout=300) == "completed"
    run = await db.fetchrow("""select q.status, q.runner, q.screenshots from qa_runs q join site_versions v on v.id=q.site_version_id
                               join generated_sites g on g.id=v.site_id where g.campaign_id=$1::uuid order by q.started_at desc limit 1""", cid)
    assert run["runner"] == "docker-sandbox" and run["status"] == "passed" and len(run["screenshots"]) >= 2


def test_same_phone_in_two_formats_is_not_a_conflict():
    from jarvis.agents.research import same_value_key
    assert same_value_key("phone", "(615) 555-0121") == same_value_key("phone", "+16155550121") == "6155550121"
    assert same_value_key("phone", "(615) 555-0121") != same_value_key("phone", "(615) 555-0199")

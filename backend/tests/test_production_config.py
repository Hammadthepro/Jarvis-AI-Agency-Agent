"""Production configuration invariants: no test doubles, no subprocess QA, runner required, compose hardening,
and restart safety of deployment (no duplicate upload)."""
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from jarvis.config import Settings

ROOT = Path(__file__).resolve().parents[2]
PROD = dict(jarvis_env="production", jarvis_encryption_key="k" * 44, supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json", qa_runner_mode="runner",
            sandbox_runner_url="http://sandbox-runner:8090", sandbox_runner_token="t" * 32)


def test_production_rejects_unsafe_settings():
    Settings(**PROD)
    for bad in ({"jarvis_enable_test_doubles": True}, {"qa_runner_mode": "subprocess"}, {"qa_runner_mode": "docker"},
                {"sandbox_runner_token": None}, {"supabase_jwks_url": None}, {"places_api_base_url": "http://evil"}):
        with pytest.raises(ValueError):
            Settings(**{**PROD, **bad})


def test_production_package_has_no_test_double_paths():
    src = "\n".join(p.read_text() for p in (ROOT / "backend" / "jarvis").rglob("*.py"))
    assert "tests.doubles" not in src and "FakeWeb" not in src and "FakeAnthropic" not in src


@pytest.mark.skipif(not shutil.which("docker"), reason="docker compose not available")
def test_compose_hardening(tmp_path):
    for f in ("app", "runner", "voice", "backup"):
        shutil.copy(ROOT / "env" / f"{f}.env.example", ROOT / "env" / f"{f}.env")
    try:
        out = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "--profile", "voice", "config", "--format", "json"],
                             capture_output=True, text=True, check=True, env={**os.environ, "API_HOSTNAME": "x", "CADDY_ACME_EMAIL": "x"})
    finally:
        for f in ("app", "runner", "voice", "backup"):
            (ROOT / "env" / f"{f}.env").unlink()
    svc = json.loads(out.stdout)["services"]
    sock = [n for n, s in svc.items() if any("docker.sock" in (v.get("source") or "") for v in s.get("volumes", []))]
    assert sock == ["sandbox-runner"]
    assert not svc["sandbox-runner"].get("ports")
    assert "docker-proxy" not in svc
    for name in ("api", "worker", "migrate", "backup", "sandbox-runner"):
        assert svc[name].get("read_only") is True and "no-new-privileges:true" in svc[name].get("security_opt", [])
    for name in ("api", "worker"):
        assert svc[name]["cap_drop"] == ["ALL"]
        assert svc[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        assert svc[name]["environment"]["QA_RUNNER_MODE"] == "runner"
    assert svc["worker"]["depends_on"]["sandbox-runner"]["condition"] == "service_healthy"
    for name in ("api", "worker", "sandbox-runner", "caddy", "backup"):
        assert svc[name]["restart"] == "unless-stopped"
    for name in ("api", "worker", "sandbox-runner"):
        assert svc[name].get("healthcheck", {}).get("test")
    assert svc["migrate"]["restart"] == "no"
    for s in svc.values():
        assert "JARVIS_ENABLE_TEST_DOUBLES" not in (s.get("environment") or {})


async def test_deploy_retry_after_crash_does_not_reupload(client, deps, web, db):
    """Crash after the Cloudflare upload but before verification: the retry adopts the upload instead of re-uploading."""
    from jarvis.agents.deployment import deploy_site
    from jarvis.integrations.base import CallContext
    from jarvis.sites.design import choose_design
    from jarvis.sites.render import render_site
    from tests.test_core_db import new_ws
    from tests.test_repair_loop import make_site
    ws, _ = await new_ws(db)
    cid, sid = await make_site(db, ws)
    out = deps.settings.sites_artifact_dir / str(sid) / "v1" / "dist"
    render_site(out, content={"business_name": "Bayou City Plumbing Co", "meta_title": "Bayou City Plumbing Co", "meta_description": "x",
                              "eyebrow": "x", "headline": "x", "subheadline": "x", "primary_cta": "Call", "services_heading": "S",
                              "services": [], "about_heading": "A", "about": [], "service_area": None, "service_area_short": None,
                              "faq": [], "cta_heading": "c", "cta_body": "c", "phone_display": None, "phone_tel": None, "email": None,
                              "address": None, "hours": None}, design=choose_design("d", "plumbing", []), agency="T", seed="d")
    spec = await db.fetchval("""insert into site_specs(workspace_id, prospect_id, spec, design, facts_used, model, prompt_version)
                                select $1, prospect_id, '{}', '{}', '{}', 'm', 'v' from generated_sites where id=$2 returning id""", ws, sid)
    vid = await db.fetchval("""insert into site_versions(workspace_id, site_id, version, spec_id, reason, artifact_path)
                               values ($1,$2,1,$3,'initial',$4) returning id""", ws, sid, spec, str(out))
    await db.execute("update generated_sites set current_version_id=$2, state='passed' where id=$1", sid, vid)
    real_fetch = web.fetch

    async def crash_on_verify(url, **kw):           # simulate the worker dying right after the upload
        if ".pages.dev" in url:
            raise RuntimeError("worker crashed")
        return await real_fetch(url, **kw)
    web.fetch = crash_on_verify
    deps.fetch = crash_on_verify
    with pytest.raises(RuntimeError):
        await deploy_site(deps, CallContext(db, ws, None, cid, sid), sid, verify_attempts=1, verify_delay_s=0)
    assert len(web.deploy_runs) == 1
    deps.fetch = real_fetch
    res = await deploy_site(deps, CallContext(db, ws, None, cid, sid), sid, verify_attempts=1, verify_delay_s=0)
    assert res["status"] == "verified" and len(web.deploy_runs) == 1      # no second upload
    assert await db.fetchval("select count(*) from deployments where site_version_id=$1", vid) == 1
    _ = uuid

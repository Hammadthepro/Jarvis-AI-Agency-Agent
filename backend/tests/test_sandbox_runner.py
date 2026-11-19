"""Sandbox runner: auth, path confinement, no runtime pulls, fixed hardened flags, and a real isolated run."""
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from jarvis.agents.qa import run_qa
from jarvis.config import Settings
from jarvis.sandbox_runner import create_app, is_pinned, sandbox_command
from jarvis.sites.design import choose_design
from jarvis.sites.render import render_site

HAVE_DOCKER = bool(shutil.which("docker")) and subprocess.run(
    ["docker", "image", "inspect", "jarvis-qa-sandbox:latest"], capture_output=True).returncode == 0
TOKEN = "runner-token-0123456789abcdef"


def build_site(root: Path) -> Path:
    site = root / "site-1" / "v1" / "dist"
    c = {"business_name": "Runner Test Co", "meta_title": "Runner Test Co", "meta_description": "Test.", "eyebrow": "Test",
         "headline": "Runner QA", "subheadline": "Testing.", "primary_cta": "Call now", "services_heading": "Services",
         "services": [{"name": "Testing", "description": "Tests."}], "about_heading": "About", "about": ["About."],
         "service_area": "Testville", "service_area_short": "Testville", "faq": [], "cta_heading": "Go", "cta_body": "Now.",
         "phone_display": "(713) 555-0142", "phone_tel": "+17135550142", "email": None, "address": None, "hours": None}
    render_site(site, content=c, design=choose_design("rn", "plumbing", []), agency="T", seed="rn")
    for p in [root, *root.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    return site


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runner")


def test_command_is_fixed_and_hardened(tmp_path):
    cmd = sandbox_command("sha256:abc", tmp_path / "s", tmp_path / "o", tmp_path / "f.json")
    for flag in ("--pull=never", "--read-only", "no-new-privileges", "--memory-swap"):
        assert flag in cmd
    assert cmd[cmd.index("--network") + 1] == "none" and cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "--privileged" not in cmd and not any("docker.sock" in c for c in cmd)
    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
    assert len(mounts) == 3 and mounts[0].endswith(":/site:ro") and not any(m.startswith("/:") for m in mounts)
    assert is_pinned("sha256:abc") and is_pinned("x@sha256:abc") and not is_pinned("jarvis-qa-sandbox:latest")


async def test_auth_and_path_confinement(tmp_path):
    root = tmp_path / "sites"
    site = build_site(root)
    app = create_app(token=TOKEN, root=root, image="jarvis-qa-sandbox:latest")
    async with client(app) as c:
        assert (await c.post("/qa", json={"site_dir": str(site)})).status_code == 401
        assert (await c.post("/qa", json={"site_dir": str(site)}, headers={"Authorization": "Bearer wrong"})).status_code == 401
        h = {"Authorization": f"Bearer {TOKEN}"}
        for bad in ("/etc", str(root / ".." / ".."), str(site / ".." / ".." / ".." / ".."), "/var/run"):
            assert (await c.post("/qa", json={"site_dir": bad}, headers=h)).status_code == 400
        (site / "evil").symlink_to("/etc/passwd")
        assert (await c.post("/qa", json={"site_dir": str(site)}, headers=h)).status_code == 400


async def test_missing_image_refused_without_pull(tmp_path):
    root = tmp_path / "sites"
    site = build_site(root)
    app = create_app(token=TOKEN, root=root, image="jarvis-qa-sandbox-missing:never-built")
    async with client(app) as c:
        r = await c.post("/qa", json={"site_dir": str(site)}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503 and "no runtime pulls" in r.text


@pytest.mark.skipif(not HAVE_DOCKER, reason="docker or jarvis-qa-sandbox:latest image not available")
async def test_real_qa_via_runner_mode(tmp_path):
    root = tmp_path / "sites"
    site = build_site(root)
    app = create_app(token=TOKEN, root=root, image="jarvis-qa-sandbox:latest")
    s = Settings(jarvis_env="test", qa_runner_mode="runner", sandbox_runner_url="http://runner", sandbox_runner_token=TOKEN)
    async with client(app) as c:
        report, runner = await run_qa(s, site, {"business_name": "Runner Test Co", "phones": ["+17135550142"]}, site.parent, http=c)
    assert runner == "sandbox-runner" and report["passed"], report["findings"]
    assert (site.parent / "qa" / "desktop.png").exists()


@pytest.mark.skipif(not HAVE_DOCKER, reason="docker or jarvis-qa-sandbox:latest image not available")
def test_inside_sandbox_no_socket_no_secrets_no_network_no_host_fs(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    cmd = sandbox_command("jarvis-qa-sandbox:latest", tmp_path, tmp_path, tmp_path / "f.json")
    (tmp_path / "f.json").write_text("{}")
    probe = ("const fs=require('fs');const out={sock:fs.existsSync('/var/run/docker.sock'),hostEtcShadow:fs.existsSync('/host'),"
             "secret:Object.keys(process.env).filter(k=>/KEY|TOKEN|SECRET|DATABASE/.test(k)),uid:process.getuid(),"
             "caps:fs.readFileSync('/proc/self/status','utf8').match(/CapEff:\\s*(\\w+)/)[1]};"
             "fetch('http://1.1.1.1').then(()=>{out.net=true}).catch(()=>{out.net=false}).finally(()=>console.log(JSON.stringify(out)))")
    idx = cmd.index("jarvis-qa-sandbox:latest")
    r = subprocess.run(cmd[:idx + 1] + ["node", "-e", probe], capture_output=True, text=True, timeout=60)
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"sock": False, "hostEtcShadow": False, "secret": [], "uid": 10001, "caps": "0000000000000000", "net": False}

"""Builds a site and runs browser QA inside the isolated Docker sandbox (network none, read-only, non-root).
Skipped (with reason) when Docker or the sandbox image is unavailable; never reported as passed in that case."""
import shutil
import subprocess

import pytest

from jarvis.agents.qa import docker_command, run_qa
from jarvis.config import Settings
from jarvis.sites.design import choose_design
from jarvis.sites.render import render_site


def _image_available() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", "jarvis-qa-sandbox:latest"], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _image_available(), reason="docker or jarvis-qa-sandbox:latest image not available")


async def test_qa_in_docker_sandbox(tmp_path):
    s = Settings(jarvis_env="test", qa_runner_mode="docker")
    content = {"business_name": "Sandbox Test Co", "meta_title": "Sandbox Test Co", "meta_description": "Test.", "eyebrow": "Test",
               "headline": "Sandbox QA", "subheadline": "Testing the sandbox.", "primary_cta": "Call now", "services_heading": "Services",
               "services": [{"name": "Testing", "description": "Running tests."}], "about_heading": "About", "about": ["About text."],
               "service_area": "Testville", "service_area_short": "Testville", "faq": [], "cta_heading": "Go", "cta_body": "Now.",
               "phone_display": "(713) 555-0142", "phone_tel": "+17135550142", "email": None, "address": None, "hours": None}
    render_site(tmp_path / "dist", content=content, design=choose_design("sbx", "plumbing", []), agency="T", seed="sbx")
    tmp_path.chmod(0o755)
    report, runner = await run_qa(s, tmp_path / "dist", {"business_name": "Sandbox Test Co", "phones": ["+17135550142"]}, tmp_path)
    assert runner == "docker-sandbox" and report["passed"], report["findings"]
    cmd = docker_command(s, tmp_path / "dist", tmp_path / "qa", tmp_path / "f.json")
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges"):
        assert flag in cmd
    assert not any("docker.sock" in c for c in cmd)

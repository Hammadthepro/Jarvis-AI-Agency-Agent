"""Agent 8: QA. Runs the Node/Playwright/axe runner against a built site.

Modes (recorded in qa_runs.runner):
* `runner`     (production) HTTP call to the sandbox runner service, the only component with Docker
               access. The worker has no Docker socket and cannot choose flags, images or mounts.
* `docker`     (development/CI host) launches the same hardened container directly.
* `subprocess` (development only) runs the runner with local Node; rejected in production by config.
All modes produce the same report; screenshots land in <artifact_dir>/qa."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx

from jarvis.config import Settings
from jarvis.sandbox_runner import sandbox_command


class QaRunnerError(Exception):
    pass


def docker_command(settings: Settings, site_dir: Path, out_dir: Path, facts: Path) -> list[str]:
    return sandbox_command(settings.sandbox_image, site_dir, out_dir, facts)


async def run_qa(settings: Settings, site_dir: Path, facts: dict, artifact_dir: Path,
                 http: httpx.AsyncClient | None = None) -> tuple[dict, str]:
    if settings.qa_runner_mode == "runner":
        if not settings.sandbox_runner_url or not settings.sandbox_runner_token:
            raise QaRunnerError("QA_RUNNER_MODE=runner requires SANDBOX_RUNNER_URL and SANDBOX_RUNNER_TOKEN (NOT_CONFIGURED)")
        client = http or httpx.AsyncClient(timeout=settings.sandbox_timeout_s + 30)
        try:
            r = await client.post(f"{settings.sandbox_runner_url}/qa", json={"site_dir": str(site_dir.resolve()), "facts": facts},
                                  headers={"Authorization": f"Bearer {settings.sandbox_runner_token.get_secret_value()}"})
        except httpx.HTTPError as exc:
            raise QaRunnerError(f"sandbox runner unreachable: {exc}") from exc
        finally:
            if http is None:
                await client.aclose()
        if r.status_code != 200:
            raise QaRunnerError(f"sandbox runner error {r.status_code}: {r.text[:500]}")
        return r.json(), "sandbox-runner"

    out_dir = artifact_dir / "qa"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    out_dir.chmod(0o777)
    facts_path = artifact_dir / "qa-facts.json"
    facts_path.write_text(json.dumps(facts))
    facts_path.chmod(0o644)
    if settings.qa_runner_mode == "docker":
        cmd, runner = docker_command(settings, site_dir.resolve(), out_dir.resolve(), facts_path.resolve()), "docker-sandbox"
    else:
        node = shutil.which("node")
        if not node:
            raise QaRunnerError("node not available for subprocess QA runner")
        cmd, runner = [node, str(settings.qa_runner_dir / "run.mjs"), str(site_dir), str(out_dir), str(facts_path)], "local-subprocess"
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.sandbox_timeout_s)
    except TimeoutError as exc:
        proc.kill()
        raise QaRunnerError("QA runner timed out") from exc
    report_path = out_dir / "report.json"
    if proc.returncode != 0 or not report_path.exists():
        raise QaRunnerError(f"QA runner failed (exit {proc.returncode}): {err.decode(errors='replace')[-800:]}")
    return json.loads(report_path.read_text()), runner

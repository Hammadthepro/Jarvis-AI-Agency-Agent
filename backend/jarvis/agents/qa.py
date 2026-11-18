"""Agent 8: QA. Runs the Node/Playwright/axe runner against a built site.

Production mode `docker`: an isolated container (--network none, non-root, read-only root fs,
dropped capabilities, no-new-privileges, CPU/memory/pids limits, no Docker socket, no secrets, the
site mounted read-only). Mode `subprocess` runs the same runner locally (development/CI only) and is
recorded as such in qa_runs.runner."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from jarvis.config import Settings


class QaRunnerError(Exception):
    pass


def docker_command(settings: Settings, site_dir: Path, out_dir: Path, facts: Path) -> list[str]:
    return ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "10001:10001",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
            "--cpus", "1", "--memory", "1g", "--tmpfs", "/tmp:rw,size=256m",  # noqa: S108 - tmpfs inside the sandbox container
            "-e", "HOME=/tmp", "-v", f"{site_dir}:/site:ro", "-v", f"{facts}:/facts.json:ro", "-v", f"{out_dir}:/out:rw",
            settings.sandbox_image, "node", "/runner/run.mjs", "/site", "/out", "/facts.json"]


async def run_qa(settings: Settings, site_dir: Path, facts: dict, artifact_dir: Path) -> tuple[dict, str]:
    out_dir = artifact_dir / "qa"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(facts, fh)
        facts_path = Path(fh.name)
    facts_path.chmod(0o644)
    out_dir.chmod(0o777)
    if settings.qa_runner_mode == "docker":
        cmd, runner = docker_command(settings, site_dir.resolve(), out_dir.resolve(), facts_path), "docker-sandbox"
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
    finally:
        facts_path.unlink(missing_ok=True)
    report_path = out_dir / "report.json"
    if proc.returncode != 0 or not report_path.exists():
        raise QaRunnerError(f"QA runner failed (exit {proc.returncode}): {err.decode(errors='replace')[-800:]}")
    return json.loads(report_path.read_text()), runner

"""Sandbox runner: the ONLY component with Docker access.

Replaces a generic Docker socket proxy (which would let any client create privileged containers with
arbitrary host mounts). It exposes a single, fixed operation over an internal network:

    POST /qa  {"site_dir": "<path under SITES_ARTIFACT_DIR>", "facts": {...}}   (Bearer SANDBOX_RUNNER_TOKEN)

and always launches the QA container with the same hardened flags: pinned image (no pull at runtime),
--network none, read-only root, non-root user, all capabilities dropped, no-new-privileges, resource
limits, and only the site directory (read-only) plus an output directory mounted. Callers cannot pass
flags, images, commands or mounts. Run: uvicorn jarvis.sandbox_runner:app --port 8090
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

SITES_ROOT = Path(os.environ.get("SITES_ARTIFACT_DIR", "/var/lib/jarvis/sites")).resolve()
IMAGE = os.environ.get("SANDBOX_IMAGE", "jarvis-qa-sandbox:latest")
TIMEOUT_S = int(os.environ.get("SANDBOX_TIMEOUT_S", "300"))


def sandbox_command(image: str, site_dir: Path, out_dir: Path, facts: Path) -> list[str]:
    return ["docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--user", "10001:10001",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
            "--cpus", "1", "--memory", "1g", "--memory-swap", "1g", "--ipc", "private",
            "--tmpfs", "/tmp:rw,size=256m,noexec,nosuid",  # noqa: S108 - tmpfs inside the sandbox container
            "-e", "HOME=/tmp", "-v", f"{site_dir}:/site:ro", "-v", f"{facts}:/facts.json:ro", "-v", f"{out_dir}:/out:rw",
            image, "node", "/runner/run.mjs", "/site", "/out", "/facts.json"]


def is_pinned(image: str) -> bool:
    return image.startswith("sha256:") or "@sha256:" in image


def confine(path: str, root: Path = SITES_ROOT) -> Path:
    """Resolve a caller-supplied path and require it to be a directory inside the sites root."""
    p = Path(path).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "site_dir must be inside the sites artifact directory")
    if not p.is_dir() or p.is_symlink():
        raise HTTPException(400, "site_dir must be an existing directory")
    for f in p.rglob("*"):
        if f.is_symlink():
            raise HTTPException(400, "symlinks are not allowed in site output")
    return p


async def image_present(image: str) -> bool:
    proc = await asyncio.create_subprocess_exec("docker", "image", "inspect", image, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


class QaIn(BaseModel):
    site_dir: str = Field(max_length=1000)
    facts: dict = Field(default_factory=dict)


def create_app(token: str | None = None, root: Path | None = None, image: str | None = None) -> FastAPI:
    app = FastAPI(title="JARVIS sandbox runner", docs_url=None, redoc_url=None, openapi_url=None)
    tok = token if token is not None else os.environ.get("SANDBOX_RUNNER_TOKEN", "")
    sites_root = (root or SITES_ROOT).resolve()
    img = image or IMAGE
    if os.environ.get("JARVIS_ENV") == "production" and not is_pinned(img):
        raise RuntimeError("SANDBOX_IMAGE must be pinned by digest in production (sha256:<id> or name@sha256:<digest>)")

    @app.get("/health")
    async def health():
        return {"status": "ok", "image_present": await image_present(img), "image": img}

    @app.post("/qa")
    async def qa(body: QaIn, authorization: str | None = Header(None)):
        if not tok or not authorization or not hmac.compare_digest(authorization, f"Bearer {tok}"):
            raise HTTPException(401, "unauthorized")
        site = confine(body.site_dir, sites_root)
        if not await image_present(img):
            raise HTTPException(503, f"sandbox image {img} not present locally; build it during provisioning (no runtime pulls)")
        work = site.parent / "qa"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        work.chmod(0o777)
        facts = site.parent / "qa-facts.json"
        facts.write_text(json.dumps(body.facts)[:100_000])
        facts.chmod(0o644)
        proc = await asyncio.create_subprocess_exec(*sandbox_command(img, site, work, facts),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _o, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        except TimeoutError as exc:
            proc.kill()
            raise HTTPException(504, "sandbox timed out") from exc
        report = work / "report.json"
        if proc.returncode != 0 or not report.exists():
            raise HTTPException(500, f"sandbox failed (exit {proc.returncode}): {err.decode(errors='replace')[-500:]}")
        return json.loads(report.read_text())

    return app


app = create_app() if os.environ.get("SANDBOX_RUNNER_TOKEN") else None

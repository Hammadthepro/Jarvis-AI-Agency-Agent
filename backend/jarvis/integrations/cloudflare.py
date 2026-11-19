"""Cloudflare Pages Direct Upload adapter.

Upload uses Cloudflare's official CLI (`wrangler pages deploy`, pinned version) which implements the
Direct Upload protocol; project management, deployment confirmation and cleanup use the REST API
(api.cloudflare.com/client/v4). A deployment is only `verified` after (1) the API reports the
deployment and (2) the pages.dev URL serves the expected content."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from jarvis.config import Settings
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call, http_request

PROVIDER = "cloudflare"
API = "https://api.cloudflare.com/client/v4"
WRANGLER = "wrangler@4.160.1"


@dataclass(frozen=True)
class DeployResult:
    deployment_id: str | None
    url: str | None
    alias_url: str | None
    raw_stdout: str


class CloudflarePagesClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None, runner=None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=60)
        self.runner = runner or self._run_wrangler

    @property
    def configured(self) -> bool:
        return bool(self.settings.cloudflare_api_token and self.settings.cloudflare_account_id)

    def _auth(self) -> dict:
        if not self.configured:
            raise not_configured(PROVIDER, "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")
        assert self.settings.cloudflare_api_token
        return {"Authorization": f"Bearer {self.settings.cloudflare_api_token.get_secret_value()}"}

    @property
    def project(self) -> str:
        return self.settings.cloudflare_pages_preview_project

    async def verify_token(self) -> dict:
        h = self._auth()
        try:
            r = await http_request(PROVIDER, self.http, "GET", f"{API}/accounts/{self.settings.cloudflare_account_id}/tokens/verify", headers=h)
        except ProviderError:
            r = await http_request(PROVIDER, self.http, "GET", f"{API}/user/tokens/verify", headers=h)
        return r.json()

    async def list_projects(self) -> list[dict]:
        """Lists Pages projects in the account (free). Confirms the account id and the token's Pages access."""
        r = await http_request(PROVIDER, self.http, "GET", f"{API}/accounts/{self.settings.cloudflare_account_id}/pages/projects",
                               headers=self._auth())
        return r.json().get("result") or []

    async def ensure_project(self) -> None:
        h = self._auth()
        acct = self.settings.cloudflare_account_id
        try:
            await http_request(PROVIDER, self.http, "GET", f"{API}/accounts/{acct}/pages/projects/{self.project}", headers=h)
        except ProviderError as exc:
            if exc.upstream_status != 404:
                raise
            await http_request(PROVIDER, self.http, "POST", f"{API}/accounts/{acct}/pages/projects", headers=h,
                               json={"name": self.project, "production_branch": "production"})

    async def _run_wrangler(self, directory: Path, branch: str) -> tuple[int, str, str]:
        npx = shutil.which("npx")
        if not npx:
            raise ProviderError(PROVIDER, IntegrationState.NOT_CONFIGURED, "npx (Node.js) not available on this host")
        assert self.settings.cloudflare_api_token
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
               "CLOUDFLARE_API_TOKEN": self.settings.cloudflare_api_token.get_secret_value(),
               "CLOUDFLARE_ACCOUNT_ID": self.settings.cloudflare_account_id or "", "WRANGLER_SEND_METRICS": "false",
               "CI": "1"}
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "npm_config_cache"):
            if k in os.environ:
                env[k] = os.environ[k]
        proc = await asyncio.create_subprocess_exec(
            npx, "--yes", WRANGLER, "pages", "deploy", str(directory), "--project-name", self.project,
            "--branch", branch, "--commit-dirty=true", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
        except TimeoutError as exc:
            proc.kill()
            raise ProviderError(PROVIDER, IntegrationState.TEMPORARILY_UNAVAILABLE, "wrangler timed out; upload state unknown",
                                billing_ambiguous=True) from exc
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def deploy(self, ctx: CallContext, directory: Path, branch: str) -> DeployResult:
        self._auth()

        async def do() -> CallOutcome:
            await self.ensure_project()
            code, out, err = await self.runner(directory, branch)
            text = out + "\n" + err
            if code != 0:
                state = IntegrationState.INVALID_CREDENTIALS if "Authentication error" in text or "10000" in text \
                    else IntegrationState.PROVIDER_ERROR
                raise ProviderError(PROVIDER, state, f"wrangler exit {code}: {text[-600:]}", retryable=False,
                                    billing_ambiguous=False)
            urls = re.findall(r"https://[a-z0-9.-]+\.pages\.dev", text)
            dep = await self.latest_deployment(branch)
            result = DeployResult(dep.get("id") if dep else None, (dep or {}).get("url") or (urls[0] if urls else None),
                                  next((u for u in urls if u.startswith(f"https://{branch}.")), None), text[-2000:])
            if not result.deployment_id:
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR,
                                    "wrangler reported success but the API shows no deployment for this branch")
            return CallOutcome(result, 1, 0)

        return await gateway_call(ctx, provider=PROVIDER, operation="pages.deploy", unit="deployment", units=1,
                                  estimated_micros=0, fn=do)

    async def latest_deployment(self, branch: str) -> dict | None:
        h = self._auth()
        r = await http_request(PROVIDER, self.http, "GET",
                               f"{API}/accounts/{self.settings.cloudflare_account_id}/pages/projects/{self.project}/deployments",
                               headers=h, params={"env": "preview"})
        for d in r.json().get("result", []):
            trig = (d.get("deployment_trigger") or {}).get("metadata") or {}
            if trig.get("branch") == branch:
                return d
        return None

    async def delete_deployment(self, deployment_id: str) -> None:
        h = self._auth()
        await http_request(PROVIDER, self.http, "DELETE",
                           f"{API}/accounts/{self.settings.cloudflare_account_id}/pages/projects/{self.project}/deployments/{deployment_id}",
                           headers=h, params={"force": "true"})


def headers_file(csp: str) -> str:
    return "\n".join(["/*", f"  Content-Security-Policy: {csp}", "  X-Robots-Tag: noindex, nofollow",
                      "  X-Content-Type-Options: nosniff", "  Referrer-Policy: no-referrer",
                      "  Permissions-Policy: camera=(), microphone=(), geolocation=()", "  X-Frame-Options: DENY", ""])


_ = json

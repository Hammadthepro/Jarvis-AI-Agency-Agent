"""Resend email adapter. Sending is only reachable through the outreach send path, which enforces
the sender-readiness gate, authorization and a suppression re-check immediately before sending.

Idempotency: Resend supports an `Idempotency-Key` header; we send the outbox key. A timeout or
connection loss after sending yields UNKNOWN_PROVIDER_RESULT, never a blind retry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

import httpx

from jarvis.config import Settings
from jarvis.core.errors import ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call, http_request

PROVIDER = "resend"
API = "https://api.resend.com"


@dataclass(frozen=True)
class SendResult:
    message_id: str


class ResendClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30)

    @property
    def configured(self) -> bool:
        return self.settings.email_provider == "resend" and self.settings.resend_api_key is not None

    def _h(self) -> dict:
        if not self.configured:
            raise not_configured(PROVIDER, "EMAIL_PROVIDER=resend and RESEND_API_KEY")
        assert self.settings.resend_api_key
        return {"Authorization": f"Bearer {self.settings.resend_api_key.get_secret_value()}"}

    async def domain_status(self, domain: str) -> dict:
        r = await http_request(PROVIDER, self.http, "GET", f"{API}/domains", headers=self._h())
        for d in r.json().get("data", []):
            if d.get("name") == domain:
                return d
        return {"name": domain, "status": "not_found"}

    async def send(self, ctx: CallContext, *, idempotency_key: str, from_addr: str, to: str, reply_to: str,
                   subject: str, text: str, headers: dict) -> SendResult:
        h = {**self._h(), "Idempotency-Key": idempotency_key}
        body = {"from": from_addr, "to": [to], "reply_to": reply_to, "subject": subject, "text": text, "headers": headers}

        async def do() -> CallOutcome:
            r = await http_request(PROVIDER, self.http, "POST", f"{API}/emails", json=body, headers=h, retries=0)
            return CallOutcome(SendResult(r.json()["id"]), 1, 0, http_status=r.status_code)

        return await gateway_call(ctx, provider=PROVIDER, operation="emails.send", unit="email", units=1,
                                  estimated_micros=0, fn=do)


def verify_svix(secret: str, msg_id: str, timestamp: str, body: bytes, signatures: str, tolerance_s: int = 300) -> bool:
    """Resend webhooks are signed with Svix: HMAC-SHA256 over '{id}.{timestamp}.{body}'."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, s.split(",", 1)[1]) for s in signatures.split() if "," in s)


_ = ProviderError

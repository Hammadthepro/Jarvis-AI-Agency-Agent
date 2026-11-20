"""Claude API adapter (official `anthropic` SDK). Requires an API key from the Claude Console;
a Claude Code subscription is not API access.

Structured outputs via `output_config.format` (JSON Schema). Worst-case cost is reserved from
max_tokens before each call; actual cost is committed from `response.usage`."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import anthropic
import structlog

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call

PROVIDER = "anthropic"
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClaudeResult:
    data: dict[str, Any]
    model: str
    tokens_in: int
    tokens_out: int


_KEYLIKE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_BILLING_CLASSES = tuple(getattr(anthropic, n) for n in (
    "PaymentRequiredError", "OrganizationOnHoldError", "EnforcedSpendLimitReachedError", "ContractSpendLimitReachedError",
    "LinkedOrganizationSpendLimitReachedError", "LinkedOrganizationBillingUnavailableError") if hasattr(anthropic, n))


def error_info(exc: Exception) -> dict[str, Any]:
    """Safe, structured description of an Anthropic failure (never contains the API key)."""
    body = getattr(exc, "body", None)
    err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    msg = err.get("message") or getattr(exc, "message", None) or str(exc)
    return {"exception": type(exc).__name__, "http_status": getattr(exc, "status_code", None), "error_type": err.get("type"),
            "message": _KEYLIKE.sub("sk-ant-***", str(msg))[:400], "request_id": getattr(exc, "request_id", None)}


def _map(exc: Exception) -> ProviderError:
    """Keeps Anthropic's distinctions (authentication, billing, permission, model access, invalid request, rate limit,
    overload/server, timeout) instead of one generic error, and logs them without secrets."""
    info = error_info(exc)
    low = info["message"].lower()
    reason = (f"{info['exception']}" + (f" HTTP {info['http_status']}" if info["http_status"] else "")
              + (f" {info['error_type']}" if info["error_type"] else "") + f": {info['message']}"
              + (f" (request id {info['request_id']})" if info["request_id"] else ""))
    log.warning("anthropic.error", **info)
    code = info["http_status"]
    S = IntegrationState
    if isinstance(exc, anthropic.AuthenticationError):
        state, retry = S.INVALID_CREDENTIALS, False
    elif isinstance(exc, _BILLING_CLASSES) or info["error_type"] == "billing_error" or "credit balance" in low or "billing" in low:
        state, retry = S.QUOTA_EXCEEDED, False
    elif isinstance(exc, anthropic.PermissionDeniedError):
        state, retry = S.PERMISSION_DENIED, False
    elif isinstance(exc, anthropic.RateLimitError):
        state, retry = S.RATE_LIMITED, True
    elif isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(PROVIDER, S.TEMPORARILY_UNAVAILABLE, f"{reason}; billing outcome unknown", billing_ambiguous=True,
                             details=info)
    elif isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(PROVIDER, S.TEMPORARILY_UNAVAILABLE, reason, retryable=True, billing_ambiguous=True, details=info)
    elif isinstance(exc, anthropic.NotFoundError):
        state, retry = S.PROVIDER_ERROR, False
        reason = "model not available to this API key or not found. " + reason
    elif isinstance(exc, anthropic.APIStatusError) and (code or 0) >= 500:
        state, retry = S.TEMPORARILY_UNAVAILABLE, True
    elif isinstance(exc, anthropic.BadRequestError):
        state, retry = S.PROVIDER_ERROR, False
        reason = "invalid request. " + reason
    else:
        state, retry = S.PROVIDER_ERROR, False
    return ProviderError(PROVIDER, state, reason, http_status=code, retryable=retry, details=info)


@dataclass(frozen=True)
class ConverseResult:
    content: list[dict[str, Any]]      # assistant content blocks as plain dicts (text / tool_use)
    stop_reason: str
    model: str



class ClaudeClient:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def configured(self) -> bool:
        return self.settings.anthropic_api_key is not None or self._client is not None

    def reconfigure(self, settings: Settings) -> None:
        """Applies new effective settings; drops a cached SDK client built from a different key."""
        old = self.settings.anthropic_api_key
        self.settings = settings
        if getattr(self, "_owned", False) and old != settings.anthropic_api_key:
            self._client, self._owned = None, False

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self.settings.anthropic_api_key:
                raise not_configured(PROVIDER, "ANTHROPIC_API_KEY (Claude Console API key)")
            self._client = anthropic.AsyncAnthropic(api_key=self.settings.anthropic_api_key.get_secret_value(),
                                                    max_retries=2, timeout=600)
            self._owned = True
        return self._client

    async def validate_model(self, model: str) -> dict:
        """Checks the model exists for this key. Never substitutes another model."""
        try:
            m = await self.client().models.retrieve(model)
        except Exception as exc:
            raise _map(exc) from exc
        return {"id": m.id, "display_name": getattr(m, "display_name", None)}

    async def structured(self, ctx: CallContext, *, model: str, system: str, user: str, schema: dict,
                         max_tokens: int, operation: str, effort: str | None = None) -> ClaudeResult:
        if model not in budget.pricing()["anthropic"]:
            raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, f"model {model} has no configured price")
        est_in = max(1, len(system) + len(user)) // 3 + 500
        worst = budget.claude_cost(model, est_in, max_tokens)
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        client = self.client()

        async def do() -> CallOutcome:
            try:
                msg = await client.messages.create(
                    model=model, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}], output_config=output_config)
            except Exception as exc:
                raise _map(exc) from exc
            if msg.stop_reason == "refusal":
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, "model refused the request")
            if msg.stop_reason == "max_tokens":
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, "output truncated at max_tokens")
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, "structured output was not valid JSON") from exc
            tin, tout = msg.usage.input_tokens, msg.usage.output_tokens
            return CallOutcome(ClaudeResult(data, msg.model, tin, tout), tin + tout,
                               budget.claude_cost(model, tin, tout), model=msg.model, tokens_in=tin, tokens_out=tout)

        return await gateway_call(ctx, provider=PROVIDER, operation=operation, unit="usd_micros", units=est_in + max_tokens,
                                  estimated_micros=worst, fn=do)

    async def converse(self, ctx: CallContext, *, model: str, system: str, messages: list[dict],
                        tools: list[dict], max_tokens: int, operation: str = "conversation.turn") -> ConverseResult:
        """One tool-using conversation step. Worst-case cost is reserved first (budget-gated like every call)."""
        if model not in budget.pricing()["anthropic"]:
            raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, f"model {model} has no configured price")
        est_in = (len(system) + len(json.dumps(messages)) + len(json.dumps(tools))) // 3 + 200
        worst = budget.claude_cost(model, est_in, max_tokens)
        client = self.client()

        async def do() -> CallOutcome:
            kw: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
            if tools:
                kw["tools"] = tools
            try:
                msg = await client.messages.create(**kw)
            except Exception as exc:
                raise _map(exc) from exc
            if msg.stop_reason == "refusal":
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, "model refused the request")
            blocks = []
            for b in msg.content:
                t = getattr(b, "type", None)
                if t == "text":
                    blocks.append({"type": "text", "text": b.text})
                elif t == "tool_use":
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})})
            tin, tout = msg.usage.input_tokens, msg.usage.output_tokens
            return CallOutcome(ConverseResult(blocks, msg.stop_reason, msg.model), tin + tout, budget.claude_cost(model, tin, tout),
                               model=msg.model, tokens_in=tin, tokens_out=tout)

        return await gateway_call(ctx, provider=PROVIDER, operation=operation, unit="usd_micros", units=est_in + max_tokens,
                                  estimated_micros=worst, fn=do)


    async def ping(self, ctx: CallContext, *, model: str) -> dict:
        """One minimal real message (a few tokens, budget-gated): proves the key, its organization's billing and access
        to `model`. Model listing alone does not prove the account can be billed."""
        return await self.converse_text(ctx, model=model, prompt="Reply with the single word: ready", max_tokens=8,
                                        operation="integration.test")

    async def converse_text(self, ctx: CallContext, *, model: str, prompt: str, max_tokens: int, operation: str) -> dict:
        res = await self.converse(ctx, model=model, system="You are a connectivity check.", messages=[{"role": "user", "content": prompt}],
                                  tools=[], max_tokens=max_tokens, operation=operation)
        text = " ".join(b["text"] for b in res.content if b["type"] == "text").strip()
        return {"model": res.model, "reply": text[:40], "stop_reason": res.stop_reason}

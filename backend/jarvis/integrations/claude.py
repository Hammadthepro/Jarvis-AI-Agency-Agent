"""Claude API adapter (official `anthropic` SDK). Requires an API key from the Claude Console;
a Claude Code subscription is not API access.

Structured outputs via `output_config.format` (JSON Schema). Worst-case cost is reserved from
max_tokens before each call; actual cost is committed from `response.usage`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import anthropic

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call

PROVIDER = "anthropic"


@dataclass(frozen=True)
class ClaudeResult:
    data: dict[str, Any]
    model: str
    tokens_in: int
    tokens_out: int


def _map(exc: Exception) -> ProviderError:
    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderError(PROVIDER, IntegrationState.INVALID_CREDENTIALS, str(exc), http_status=401)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderError(PROVIDER, IntegrationState.PERMISSION_DENIED, str(exc), http_status=403)
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError(PROVIDER, IntegrationState.RATE_LIMITED, str(exc), http_status=429, retryable=True)
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(PROVIDER, IntegrationState.TEMPORARILY_UNAVAILABLE, "timeout; billing outcome unknown",
                             billing_ambiguous=True)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(PROVIDER, IntegrationState.TEMPORARILY_UNAVAILABLE, str(exc), retryable=True,
                             billing_ambiguous=True)
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, f"model or resource not found: {exc}", http_status=404)
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code
        state = IntegrationState.TEMPORARILY_UNAVAILABLE if code in (500, 529, 503) else IntegrationState.PROVIDER_ERROR
        return ProviderError(PROVIDER, state, str(exc), http_status=code, retryable=code >= 500)
    return ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, str(exc))


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

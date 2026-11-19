"""Typed integration states and errors. Every provider failure maps to one of these;
nothing is silently converted into success."""

from __future__ import annotations

from enum import StrEnum


class IntegrationState(StrEnum):
    CONNECTED = "CONNECTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    DISABLED = "DISABLED"
    BLOCKED_PENDING_APPROVAL = "BLOCKED_PENDING_APPROVAL"


class JarvisError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500
    retryable = False

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationFailed(JarvisError):
    code, http_status = "VALIDATION_ERROR", 400


class NotFound(JarvisError):
    code, http_status = "NOT_FOUND", 404


class Forbidden(JarvisError):
    code, http_status = "FORBIDDEN", 403


class InvalidTransition(JarvisError):
    code, http_status = "INVALID_TRANSITION", 409


class ComplianceBlocked(JarvisError):
    code, http_status = "COMPLIANCE_BLOCKED", 403


class BudgetBlocked(JarvisError):
    code, http_status = "BUDGET_BLOCKED", 402


class DatabaseUnavailable(JarvisError):
    code, http_status, retryable = "DATABASE_UNAVAILABLE", 503, True


class ProviderError(JarvisError):
    """Raised by adapters. `state` tells the UI and retry policy what happened."""

    code, http_status = "PROVIDER_ERROR", 502

    def __init__(self, provider: str, state: IntegrationState, message: str, *,
                 http_status: int | None = None, retryable: bool | None = None,
                 billing_ambiguous: bool = False) -> None:
        super().__init__(f"{provider}: {state}: {message}")
        self.reason = message            # the provider's own explanation, without the provider/state prefix
        self.provider = provider
        self.state = state
        self.upstream_status = http_status
        self.billing_ambiguous = billing_ambiguous
        self.retryable = retryable if retryable is not None else state in {
            IntegrationState.RATE_LIMITED, IntegrationState.TEMPORARILY_UNAVAILABLE}
        self.code = state.value
        self.http_status = {
            IntegrationState.NOT_CONFIGURED: 424, IntegrationState.QUOTA_EXCEEDED: 429,
            IntegrationState.RATE_LIMITED: 429, IntegrationState.BLOCKED_PENDING_APPROVAL: 403,
            IntegrationState.DISABLED: 403,
        }.get(state, 502)


def not_configured(provider: str, what: str) -> ProviderError:
    return ProviderError(provider, IntegrationState.NOT_CONFIGURED, f"missing credential: {what}")

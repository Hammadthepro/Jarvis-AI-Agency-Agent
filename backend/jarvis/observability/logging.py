"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import logging
import re

import structlog

_SECRET_KEYS = re.compile(r"(key|token|secret|password|authorization|api_key)", re.I)
_QS = re.compile(r"(api_key|key|token)=([^&\s]+)", re.I)


def _redact(_logger, _name, event: dict) -> dict:
    for k, v in list(event.items()):
        if _SECRET_KEYS.search(k) and k not in ("event",):
            event[k] = "***"
        elif isinstance(v, str):
            event[k] = _QS.sub(r"\1=***", v)
    return event


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso"), _redact, structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)))

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import settings

_REDACTION_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|token|password|secret|signature)"
        r"([\"']?\s*[:=]\s*[\"']?)[^,\s\"']+"
    ),
)
_configured = False


def redact(value: str) -> str:
    redacted = value
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                match.group(1) + match.group(2) + "[REDACTED]"
                if match.lastindex and match.lastindex >= 2
                else "[REDACTED]"
            ),
            redacted,
        )
    return redacted


class JsonRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if span_context.is_valid:
            payload["trace_id"] = f"{span_context.trace_id:032x}"
            payload["span_id"] = f"{span_context.span_id:016x}"
        for key in ("run_id", "event_code", "reaped_runs", "removed_scratchpad_files"):
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonRedactingFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "celery",
        "celery.task",
        "celery.worker",
    ):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def configure_observability(app: Any | None = None) -> None:
    global _configured
    if _configured:
        return

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "haas", "deployment.environment": settings.haas_profile}
        )
    )
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
            )
        )
    trace.set_tracer_provider(provider)

    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="livez,readyz,healthz")
    HTTPXClientInstrumentor().instrument()
    CeleryInstrumentor().instrument()
    _configured = True

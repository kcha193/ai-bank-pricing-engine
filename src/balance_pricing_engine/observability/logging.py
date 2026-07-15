"""Structured logging and lightweight execution metrics.

Uses only the standard library. No raw configuration secrets, full
customer/grid dataframes, or config YAML bodies are ever logged — only
correlation/decision/run identifiers and small scalar summaries.

A future OpenTelemetry integration can wrap ``StageTimer``/``ExecutionMetrics``
to also emit spans/metrics to an enterprise monitoring backend without
changing call sites (see docs/deployment.md).
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator


class JsonLogFormatter(logging.Formatter):
    """Formats log records as single-line JSON for structured log ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "structured_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_json_logging(level: int = logging.INFO) -> None:
    """Attach a single JSON-formatted stream handler to the package root logger."""
    root = logging.getLogger("balance_pricing_engine")
    root.setLevel(level)
    if any(isinstance(h.formatter, JsonLogFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.propagate = False


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log line with arbitrary correlation/decision fields."""
    logger.log(level, message, extra={"structured_fields": fields})


@dataclass
class ExecutionMetrics:
    """Per-request execution metadata for observability, not billing/metrics export."""

    correlation_id: str
    decision_id: str | None = None
    run_id: str | None = None
    stage_durations_seconds: dict[str, float] = field(default_factory=dict)
    total_duration_seconds: float | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@contextmanager
def stage_timer(metrics: ExecutionMetrics, stage_name: str) -> Iterator[None]:
    """Record wall-clock duration of a pricing-service stage onto ExecutionMetrics."""
    start = time.monotonic()
    try:
        yield
    finally:
        metrics.stage_durations_seconds[stage_name] = time.monotonic() - start

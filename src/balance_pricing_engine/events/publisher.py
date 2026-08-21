"""Event publisher seam: in-process today, swappable for a broker later.

A future Kafka/Pub/Sub/Event Hubs adapter implements the same ``EventPublisher``
protocol (``publish(event) -> None``) and can be injected into
``PricingService`` without changing application-layer code. See
docs/adr/0004-in-process-events-before-streaming-platform.md.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Protocol

from balance_pricing_engine.events.models import DomainEvent

logger = logging.getLogger(__name__)

DEFAULT_PUBLISH_DEADLINE_SECONDS = 2.0
_PUBLISH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="event-publish")


class EventPublisher(Protocol):
    """Minimal publishing interface the application layer depends on."""

    def publish(self, event: DomainEvent) -> None: ...


class NoOpEventPublisher:
    """Default publisher: discards events. Safe when no integration is wired up."""

    def publish(self, event: DomainEvent) -> None:
        return None


class InMemoryEventPublisher:
    """Collects published events in memory. Intended for tests and local inspection."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    def publish(self, event: DomainEvent) -> None:
        self.events.append(event)


class JSONLEventPublisher:
    """Appends each event as one JSON line to a local audit file.

    This is a local integration/audit seam, not a durable event log. A future
    streaming adapter (Kafka/Pub/Sub/Event Hubs) replaces this class behind the
    same ``EventPublisher`` protocol without any application-layer changes.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def publish(self, event: DomainEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.to_json())
            handle.write("\n")


class KafkaEventPublisher:
    """Publishes events to a Kafka topic. Requires the [kafka] optional extra.

    The documented upgrade path from JSONLEventPublisher
    (docs/adr/0004-in-process-events-before-streaming-platform.md). ``publish`` blocks on
    the send future's broker ack and raises on failure or timeout; swallowing failures is
    ``publish_safely``'s job, not this class's.

    This is best-effort, not durable at-least-once delivery: every ``PricingService`` call
    routes through ``publish_safely()``, which logs a broker error or ack timeout rather
    than retrying it, and a process crash before the ack leaves no recoverable copy either
    way. A durable outbox/retry layer would be needed to close that gap; this adapter
    intentionally never blocks or fails the pricing decision it accompanies.
    """

    def __init__(
        self,
        topic: str,
        bootstrap_servers: str | list[str] = "localhost:9092",
        producer: Any | None = None,
        ack_timeout_seconds: float = 10.0,
        **producer_kwargs: Any,
    ) -> None:
        self.topic = topic
        self._ack_timeout_seconds = ack_timeout_seconds
        self._producer = producer or self._build_producer(bootstrap_servers, **producer_kwargs)

    @staticmethod
    def _build_producer(bootstrap_servers: str | list[str], **producer_kwargs: Any) -> Any:
        try:
            from kafka import KafkaProducer
        except ImportError as exc:
            raise ImportError(
                "KafkaEventPublisher requires kafka-python. "
                "Install it with: pip install 'balance_pricing_engine[kafka]'"
            ) from exc
        return KafkaProducer(
            bootstrap_servers=bootstrap_servers,
            value_serializer=lambda v: v.encode("utf-8"),
            **producer_kwargs,
        )

    def publish(self, event: DomainEvent) -> None:
        future = self._producer.send(self.topic, value=event.to_json())
        future.get(timeout=self._ack_timeout_seconds)


def publish_safely(
    publisher: EventPublisher,
    event: DomainEvent,
    *,
    deadline_seconds: float = DEFAULT_PUBLISH_DEADLINE_SECONDS,
) -> None:
    """Publish an event, logging (not raising) on failure — and never blocking the
    pricing path past `deadline_seconds`, regardless of how long the publisher takes.

    Publishing integration events is an optional side-channel. A failure — or a slow or
    hung publisher, e.g. a Kafka broker that stops acknowledging sends — must never fail,
    block, or alter the pricing decision it describes. `PricingService` calls this once
    per event and several times per request, so without a deadline a single unresponsive
    publisher could stall every request that publishes events by several times
    `KafkaEventPublisher`'s own (much longer) ack timeout. The publish runs on a shared
    worker thread; if it hasn't finished within the deadline this returns anyway and the
    publish keeps running in the background — its eventual outcome is still logged, just
    not waited on.
    """
    future = _PUBLISH_EXECUTOR.submit(publisher.publish, event)
    try:
        future.result(timeout=deadline_seconds)
    except FutureTimeoutError:
        future.add_done_callback(lambda f: _log_late_outcome(f, event))
        logger.warning(
            "Event publish exceeded %.1fs deadline; continuing without waiting "
            "event_type=%s correlation_id=%s",
            deadline_seconds,
            event.event_type,
            event.correlation_id,
        )
    except Exception:  # noqa: BLE001 - publishing failures are non-fatal by design
        logger.warning(
            "Failed to publish event event_type=%s correlation_id=%s",
            event.event_type,
            event.correlation_id,
            exc_info=True,
        )


def _log_late_outcome(future: Future[None], event: DomainEvent) -> None:
    """Log a background publish's failure if it finishes after publish_safely stopped waiting."""
    exc = future.exception()
    if exc is not None:
        logger.warning(
            "Backgrounded event publish failed event_type=%s correlation_id=%s",
            event.event_type,
            event.correlation_id,
            exc_info=exc,
        )

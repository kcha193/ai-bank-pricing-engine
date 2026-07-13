"""Event publisher seam: in-process today, swappable for a broker later.

A future Kafka/Pub/Sub/Event Hubs adapter implements the same ``EventPublisher``
protocol (``publish(event) -> None``) and can be injected into
``PricingService`` without changing application-layer code. See
docs/adr/0004-in-process-events-before-streaming-platform.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from balance_pricing_engine.events.models import DomainEvent

logger = logging.getLogger(__name__)


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


def publish_safely(publisher: EventPublisher, event: DomainEvent) -> None:
    """Publish an event, logging (not raising) on failure.

    Publishing integration events is an optional side-channel. A failure here
    must never fail or alter the pricing decision it describes.
    """
    try:
        publisher.publish(event)
    except Exception:  # noqa: BLE001 - publishing failures are non-fatal by design
        logger.warning(
            "Failed to publish event event_type=%s correlation_id=%s",
            event.event_type,
            event.correlation_id,
            exc_info=True,
        )

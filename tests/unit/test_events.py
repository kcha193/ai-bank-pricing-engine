"""Tests for domain events and the event-publisher seam."""

import json
import sys

import pytest

from balance_pricing_engine.events.models import (
    DomainEvent,
    model_resolved,
    pricing_decision_produced,
    pricing_scenario_requested,
)
from balance_pricing_engine.events.publisher import (
    InMemoryEventPublisher,
    JSONLEventPublisher,
    KafkaEventPublisher,
    NoOpEventPublisher,
    publish_safely,
)


class _FailingPublisher:
    def publish(self, event: DomainEvent) -> None:
        raise RuntimeError("simulated publisher outage")


class _FakeFuture:
    def __init__(self, *, raises: Exception | None = None):
        self._raises = raises
        self.get_called_with_timeout: float | None = None

    def get(self, timeout=None):
        self.get_called_with_timeout = timeout
        if self._raises is not None:
            raise self._raises
        return None


class _FakeKafkaProducer:
    def __init__(self, *, raises: Exception | None = None):
        self.sent: list[tuple[str, str]] = []
        self._raises = raises
        self.last_future: _FakeFuture | None = None

    def send(self, topic, value):
        self.sent.append((topic, value))
        self.last_future = _FakeFuture(raises=self._raises)
        return self.last_future


def test_domain_event_has_stable_envelope_fields():
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    assert event.event_type == "PricingScenarioRequested"
    assert event.correlation_id == "corr-1"
    assert event.payload["scenario_name"] == "baseline"
    assert event.event_version == 1
    assert event.event_id  # non-empty UUID string
    assert event.timestamp


def test_domain_event_to_json_round_trips():
    event = pricing_decision_produced(
        "corr-1",
        decision_id="dec-1",
        scenario_name="baseline",
        decision_status="RECOMMENDED",
        best_loan_rate=0.08,
        best_deposit_rate=0.05,
        objective_value=1_000_000.0,
    )

    payload = json.loads(event.to_json())

    assert payload["event_type"] == "PricingDecisionProduced"
    assert payload["decision_id"] == "dec-1"
    assert payload["payload"]["best_loan_rate"] == 0.08


def test_model_resolved_event_carries_product_and_source():
    event = model_resolved(
        "corr-1",
        decision_id="dec-1",
        product="loan",
        response_source="deterministic_synthetic",
        model_name=None,
        used_fallback=True,
    )

    assert event.payload["product"] == "loan"
    assert event.payload["used_fallback"] is True


def test_noop_publisher_discards_events():
    publisher = NoOpEventPublisher()
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    publisher.publish(event)  # must not raise


def test_in_memory_publisher_collects_events():
    publisher = InMemoryEventPublisher()
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    publisher.publish(event)

    assert publisher.events == [event]


def test_jsonl_publisher_appends_one_line_per_event(tmp_path):
    path = tmp_path / "events.jsonl"
    publisher = JSONLEventPublisher(path)
    first = pricing_scenario_requested("corr-1", scenario_name="baseline")
    second = pricing_scenario_requested("corr-2", scenario_name="stress")

    publisher.publish(first)
    publisher.publish(second)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["correlation_id"] == "corr-1"
    assert json.loads(lines[1])["correlation_id"] == "corr-2"


def test_publish_safely_swallows_publisher_failures(caplog):
    publisher = _FailingPublisher()
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    with caplog.at_level("WARNING"):
        publish_safely(publisher, event)  # must not raise

    assert any("Failed to publish event" in record.message for record in caplog.records)


def test_publish_safely_calls_through_on_success():
    publisher = InMemoryEventPublisher()
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    publish_safely(publisher, event)

    assert publisher.events == [event]


@pytest.mark.parametrize("publisher_cls", [NoOpEventPublisher, InMemoryEventPublisher])
def test_publishers_satisfy_the_protocol_signature(publisher_cls):
    publisher = publisher_cls()
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    result = publisher.publish(event)

    assert result is None


def test_kafka_publisher_sends_event_json_to_configured_topic():
    producer = _FakeKafkaProducer()
    publisher = KafkaEventPublisher(topic="pricing-events", producer=producer)
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    result = publisher.publish(event)

    assert result is None
    assert producer.sent == [("pricing-events", event.to_json())]


def test_kafka_publisher_blocks_on_send_future_ack():
    producer = _FakeKafkaProducer()
    publisher = KafkaEventPublisher(topic="pricing-events", producer=producer)
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    publisher.publish(event)

    assert producer.last_future.get_called_with_timeout is not None


def test_kafka_publisher_propagates_send_failures():
    producer = _FakeKafkaProducer(raises=RuntimeError("broker unavailable"))
    publisher = KafkaEventPublisher(topic="pricing-events", producer=producer)
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    with pytest.raises(RuntimeError, match="broker unavailable"):
        publisher.publish(event)


def test_kafka_publisher_raises_import_error_when_library_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "kafka", None)

    with pytest.raises(ImportError, match="kafka-python"):
        KafkaEventPublisher(topic="pricing-events")


def test_kafka_publisher_satisfies_the_protocol_signature():
    producer = _FakeKafkaProducer()
    publisher = KafkaEventPublisher(topic="pricing-events", producer=producer)
    event = pricing_scenario_requested("corr-1", scenario_name="baseline")

    result = publisher.publish(event)

    assert result is None

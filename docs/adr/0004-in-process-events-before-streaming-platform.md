# ADR 0004: In-Process Events Before a Streaming Platform

## Status

Accepted (2026-07-13).

## Decision

Introduce a small `EventPublisher` protocol (`events/publisher.py`) and a set of immutable,
serialisable domain events (`events/models.py`): `PricingScenarioRequested`,
`PricingDecisionProduced`, `GovernanceReviewRequired`, `PricingDecisionBlocked`,
`ModelResolved`, `RunLedgerWritten`. Ship three implementations — `NoOpEventPublisher`
(default), `InMemoryEventPublisher` (tests), `JSONLEventPublisher` (local audit file) — and
inject the publisher into `PricingService` rather than hard-coding a transport. Do not
introduce Kafka, Pub/Sub, or Event Hubs.

## Context

The target architecture asks for "event-driven integration seams" so that other systems
could eventually react to pricing decisions (e.g. notify a downstream rate-publishing
system, or feed an external audit log) without coupling the pricing engine to a specific
message broker. There is currently no consumer for these events and no requirement for
durable, at-least-once delivery — the run ledger and decision record already provide the
durable, auditable record of what happened.

## Alternatives considered

1. **Kafka (or Pub/Sub/Event Hubs) now**: real durability, real consumers, ordering
   guarantees, replay.
2. **In-process publisher protocol + local implementations** (chosen): a `Protocol` with
   `publish(event) -> None`; `NoOpEventPublisher`, `InMemoryEventPublisher`,
   `JSONLEventPublisher`.
3. **No events at all**: rely solely on the run ledger and decision record.

## Trade-offs

| | Kafka/Pub/Sub now | In-process publisher (chosen) | No events |
| --- | --- | --- | --- |
| Operational cost | New running infrastructure (brokers, topics, consumer groups) | None | None |
| Durability/at-least-once delivery | Yes | No — `JSONLEventPublisher` is a local audit file, not a durable log; a process crash between event construction and file flush can drop an event | N/A |
| Integration readiness | Immediate for real consumers | Requires implementing the same protocol against a real broker later | None — a future consumer has nothing to attach to |
| Failure isolation | Broker-dependent | Publishing failures are caught and logged (`publish_safely`); they never fail or alter the pricing decision they describe | N/A |
| Migration cost later | N/A | Low — swap the injected `EventPublisher` implementation; `PricingService` code does not change | High — events would need to be added to every call site later |

## Consequences

- Every event carries a minimal, non-sensitive payload (identifiers, decision status,
  rates, a handful of scalars) — never a full synthetic customer dataframe or raw
  configuration. This bounds what any future consumer (or this local JSONL file) can leak.
- Publishing is best-effort from the pricing decision's point of view:
  `events.publisher.publish_safely()` catches and logs any exception from
  `publisher.publish()` rather than propagating it. A failing or misconfigured event
  publisher must never fail, block, or alter a pricing decision — this is enforced by a
  test (`FailingEventPublisher` in the application-layer test suite).
- `JSONLEventPublisher` is explicitly documented as a local integration/audit seam, not a
  durable event log, in both its docstring and `docs/deployment.md`.
- A future streaming adapter implements `EventPublisher.publish(event: DomainEvent) ->
  None` against Kafka/Pub/Sub/Event Hubs and is injected into `PricingScenarioRequest`
  exactly like `JSONLEventPublisher` is today — no application-layer code changes.

## Conditions that would justify moving to Kafka (or similar)

- A real downstream consumer exists that needs at-least-once delivery, ordering, or replay
  guarantees the local implementations do not provide.
- Event volume or consumer count grows beyond what a single local file/audit log can
  usefully serve.
- Multiple services (beyond this single deployable) need to react to pricing events
  asynchronously and independently of this process's lifetime.
- Regulatory or operational requirements mandate a durable, replayable event log distinct
  from the run-ledger/decision-record files already produced.

Until then, an in-process protocol with local implementations satisfies the integration
seam requirement without paying for infrastructure this prototype does not yet need.

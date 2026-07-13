# ADR 0001: Modular Monolith Before Microservices

## Status

Accepted (2026-07-13).

## Decision

Structure the engine as a modular monolith: one deployable Python package with clear
internal package boundaries (`application/`, `governance/`, `features/`, `events/`,
`scenarios/`, `optimization/`, `modelling/`, `api/`), rather than splitting pricing,
governance, feature serving, and event publication into separately deployed services.

## Context

This project is a synthetic, portfolio-level (one home-loan rate, one term-deposit rate)
pricing prototype run mostly as a batch/CLI workflow, with an optional, low-traffic FastAPI
endpoint. There is no current requirement for independent scaling of any sub-capability,
no multi-team ownership boundary that needs a network seam to enforce, and no production
traffic volume that a single process cannot handle. The task driving this ADR
(CLAUDE.md's target architecture) explicitly asks for boundaries that *could* be deployed
separately later, without introducing permanently running infrastructure now.

## Alternatives considered

1. **Separate microservices per capability** (pricing API, governance service, feature
   service, event broker) communicating over HTTP/gRPC/a message bus.
2. **Modular monolith** (this decision): one process, clear package boundaries, dependency
   injection at the application layer (`PricingService` takes a registry, an event
   publisher, and a feature store as constructor/request arguments).
3. **Big ball of mud**: keep calling `run_baseline_diagnostics()` directly from every
   caller with no orchestration seam (the prior state).

## Trade-offs

| | Microservices now | Modular monolith (chosen) | Status quo (no seam) |
| --- | --- | --- | --- |
| Operational complexity | High (network, service discovery, distributed tracing) | Low (single process, single deploy) | Low, but no governance consistency |
| Independent scaling | Yes | No (not needed today) | No |
| Team ownership boundaries | Enforced by network seam | Enforced by code review + package structure | None |
| Latency | Added network hops | In-process calls | In-process calls |
| Migration cost later | N/A (already there) | Moderate — package boundaries are already service-shaped | High — logic is scattered across callers |

## Consequences

- `PricingService` (application layer) is the single orchestration point; API, CLI, and
  example scripts all depend on it rather than duplicating orchestration logic.
- Package boundaries (`governance/`, `features/`, `events/`) are drawn as if they might
  become separate services: they communicate through typed contracts
  (`GateResult`, `FeatureLineage`, `DomainEvent`), not shared mutable state, and take
  their collaborators (registry, feature store, event publisher) via dependency injection.
- No network calls, message broker, or service mesh exist today. Everything runs in one
  Python process and one Docker container.
- Extracting any package into its own service later means: give it an HTTP/gRPC
  interface implementing the same Python protocol it already exposes, and swap the
  in-process call for a client. The contracts were designed so this substitution should
  not require changing the application layer's calling code.

## Conditions that would justify moving to separate microservices

- Feature resolution or model scoring becomes CPU/latency-heavy enough to need
  independent horizontal scaling from the pricing API.
- A second, independent team owns governance policy and needs to deploy gate changes
  without redeploying the whole pricing engine.
- Real (non-synthetic) traffic volume exceeds what a single-process deployment can serve
  with acceptable latency, after profiling shows the API layer (not the batch grid search)
  is the bottleneck.
- Multiple products beyond home loans/term deposits are added and need genuinely
  independent release cadences.

Until one of these is true, the modular monolith is the correct choice: it captures the
architectural seams without paying for distributed-systems complexity that current scale
does not need.

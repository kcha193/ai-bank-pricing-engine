"""Resource and filesystem boundaries for untrusted API configuration only.

Trusted CLI workflows retain EngineConfig's existing capacity. Budgets count the
worst-case two expansion rounds, including repeated evaluation of earlier pairs.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass, fields
from typing import Mapping

import yaml
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent

from balance_pricing_engine.competitor_rates.models import CompetitorRatesConfig
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import build_default_sensitivity_cases
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

# Both API routes use the domain default. Request YAML cannot change this control.
API_BOUNDARY_EXPANSIONS = 2

# Error locations may contain arbitrary mapping keys. Reflect only field names
# declared by the server's schema, never raw locations, values or exception text.
_CONFIG_SCHEMA = EngineConfig.model_json_schema()
_SAFE_FIELD_NAMES = frozenset(
    name
    for schema in (_CONFIG_SCHEMA, *_CONFIG_SCHEMA.get("$defs", {}).values())
    for name in schema.get("properties", {})
)


class APIConfigError(ValueError):
    """Public-safe validation error, containing no submitted values or server paths."""

    def __init__(self, message: str, code: str = "VALIDATION_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class APILimits:
    max_request_bytes: int = 524_288
    max_yaml_bytes: int = 65_536
    max_yaml_depth: int = 24
    max_yaml_nodes: int = 4096
    max_loan_customers: int = 25_000
    max_deposit_customers: int = 25_000
    max_grid_points: int = 501
    max_grid_size: int = 25_000
    max_adaptive_evaluations: int = 40_000
    max_sensitivity_evaluations: int = 500_000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> APILimits:
        env = os.environ if env is None else env
        values = {}
        for field in fields(cls):
            variable = f"PRICING_ENGINE_API_{field.name.upper()}"
            try:
                value = int(env.get(variable, str(field.default)))
            except ValueError:
                raise ValueError(f"{variable} must be a positive integer.") from None
            if value <= 0:
                raise ValueError(f"{variable} must be a positive integer.")
            values[field.name] = value
        # Bound recursion even if an operator accidentally raises the YAML limit.
        if values["max_yaml_depth"] > 64:
            raise ValueError("PRICING_ENGINE_API_MAX_YAML_DEPTH cannot exceed 64.")
        return cls(**values)


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise APIConfigError("YAML mapping keys must be unique strings.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_api_config(config_yaml: str, *, include_sensitivity: bool = False) -> EngineConfig:
    """Reject malformed/expensive requests before customer or grid allocation."""
    limits = APILimits.from_env()
    try:
        size = len(config_yaml.encode("utf-8"))
    except UnicodeError:
        raise APIConfigError("Configuration must contain valid UTF-8 text.") from None
    if size > limits.max_yaml_bytes:
        raise APIConfigError("Configuration YAML size exceeds the API limit.")
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(config_yaml), start=1):
            if count > limits.max_yaml_nodes:
                raise APIConfigError("YAML node count exceeds the API limit.")
            if isinstance(event, AliasEvent):
                raise APIConfigError("YAML aliases are not supported by the API.")
            if isinstance(event, CollectionStartEvent):
                depth += 1
                if depth > limits.max_yaml_depth:
                    raise APIConfigError("YAML nesting exceeds the API limit.")
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
        raw = yaml.load(config_yaml, Loader=_UniqueKeyLoader)
        if not isinstance(raw, dict):
            raise APIConfigError("Configuration must be a YAML mapping.")
        _reject_path_overrides(raw)
        config = EngineConfig.model_validate(raw)
    except APIConfigError:
        raise
    except ValidationError as exc:
        locations = {
            ".".join(part for part in error["loc"] if part in _SAFE_FIELD_NAMES)
            for error in exc.errors(include_url=False, include_context=False, include_input=False)[
                :5
            ]
        }
        fields_message = ", ".join(sorted(location for location in locations if location))
        raise APIConfigError(
            f"Invalid configuration fields: {fields_message or 'configuration'}. "
            "Check required fields and finite inputs."
        ) from None
    except (yaml.YAMLError, ValueError, TypeError, RecursionError, OverflowError):
        raise APIConfigError(
            "Invalid configuration: check YAML structure, required fields and finite inputs."
        ) from None
    validate_workload(config, include_sensitivity=include_sensitivity, limits=limits)
    return config


def _reject_path_overrides(raw: dict) -> None:
    # Accept the canonical value verbatim so existing configs/base.yaml clients work.
    # Never resolve, normalise or probe a client path, even if ingestion is disabled.
    for section, name, canonical in (
        ("competitor_rates", "source_file", CompetitorRatesConfig().source_file),
        ("pricing_intelligence", "database_path", PricingIntelligenceConfig().database_path),
    ):
        value = raw.get(section)
        if isinstance(value, dict) and name in value and value[name] != canonical:
            raise APIConfigError(
                f"Client overrides of {section}.{name} are not permitted.",
                code="COMPETITOR_RATE_VALIDATION_ERROR"
                if section == "competitor_rates"
                else "VALIDATION_ERROR",
            )


def _initial_point_count(start: float, stop: float, step: float, limits: APILimits) -> int:
    if step <= 0:
        raise APIConfigError("Rate-grid step is below the API numeric precision limit.")
    quotient = (stop - start) / step
    if not math.isfinite(quotient) or quotient >= limits.max_grid_points:
        raise APIConfigError("Rate-grid dimension exceeds the API point limit.")
    if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-12):
        raise APIConfigError("Rate-grid step must land exactly on its end point.")
    return math.ceil(quotient) + 1


def _dimension_counts(start: float, stop: float, step: float, limits: APILimits) -> list[int]:
    counts = [_initial_point_count(start, stop, step, limits)]
    span = stop - start
    # The domain rounds rates to six decimals. A fractional micro-rate step may
    # round down, making expansion denser than the requested step. Bound that
    # density and allow two rounding units at the initial endpoints. No arrays
    # are allocated here, including for subnormal steps or overflowing ranges.
    span_units = span * 1_000_000
    step_units = step * 1_000_000
    if not math.isfinite(span_units) or not math.isfinite(step_units):
        raise APIConfigError("Rate-grid range exceeds the API numeric limit.")
    minimum_step_units = max(1, math.floor(math.nextafter(step_units, -math.inf)))
    intervals = math.ceil((math.ceil(span_units) + 2) / minimum_step_units)
    for _ in range(API_BOUNDARY_EXPANSIONS):
        intervals = 2 * intervals + 2
        count = intervals + 1
        if count > limits.max_grid_points:
            raise APIConfigError("Adaptive rate-grid dimension exceeds the API point limit.")
        counts.append(count)
    return counts


def validate_workload(
    config: EngineConfig, *, include_sensitivity: bool, limits: APILimits
) -> None:
    """Bound every pricing mode and the default sensitivity batch conservatively."""
    if config.simulation.n_loan_customers > limits.max_loan_customers:
        raise APIConfigError("simulation.n_loan_customers exceeds the API limit.")
    if config.simulation.n_deposit_customers > limits.max_deposit_customers:
        raise APIConfigError("simulation.n_deposit_customers exceeds the API limit.")
    if config.segmented_pricing is not None:
        if include_sensitivity:
            raise APIConfigError("Sensitivity analysis is not supported for segmented pricing.")
        loan_grids = [tier.rate_grid for tier in config.segmented_pricing.loan_tiers]
        deposit_grids = [tier.rate_grid for tier in config.segmented_pricing.deposit_tiers]
        dimensions = [
            [_dimension_counts(grid.start, grid.stop, grid.step, limits) for grid in grids]
            for grids in (loan_grids, deposit_grids)
        ]
        # A product of sums safely exceeds the sequential tier optimiser's work.
        loan_counts, deposit_counts = [
            [
                sum(counts[round_] for counts in grids)
                for round_ in range(API_BOUNDARY_EXPANSIONS + 1)
            ]
            for grids in dimensions
        ]
    elif config.pricing_strategy is not None:
        dimensions = []
        for grid in (
            config.pricing_strategy.home_loan_spread_bps,
            config.pricing_strategy.term_deposit_spread_bps,
        ):
            # The domain allocates the original bps grid before converting it.
            # Subtract before conversion: separately converted endpoints can
            # collapse to the same float and conceal millions of initial points.
            initial = _initial_point_count(grid.min, grid.max, grid.step, limits)
            counts = _dimension_counts(
                0.0, (grid.max - grid.min) / 10_000, grid.step / 10_000, limits
            )
            counts[0] = initial
            dimensions.append(counts)
        loan_counts, deposit_counts = dimensions
    else:
        loan_counts, deposit_counts = [
            _dimension_counts(grid.start, grid.stop, grid.step, limits)
            for grid in (config.products.loan_rate_grid, config.products.deposit_rate_grid)
        ]
    pairs = [loan * deposit for loan, deposit in zip(loan_counts, deposit_counts)]
    if max(pairs) > limits.max_grid_size:
        raise APIConfigError("Combined rate-grid size, including expansion, exceeds the API limit.")
    adaptive_work = sum(pairs)
    if adaptive_work > limits.max_adaptive_evaluations:
        raise APIConfigError("Adaptive expansion workload exceeds the API limit.")
    if include_sensitivity:
        # Include the endpoint's main run as well as every sensitivity case.
        runs = 1 + len(build_default_sensitivity_cases(config))
        if adaptive_work * runs > limits.max_sensitivity_evaluations:
            raise APIConfigError("Sensitivity-analysis workload exceeds the API limit.")


class RequestSizeLimitMiddleware:
    """Cap actual ASGI body bytes before JSON parsing, including chunked requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        try:
            limit = APILimits.from_env().max_request_bytes
        except ValueError:
            await self._reject(scope, receive, send, 500, "API resource limits are misconfigured.")
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await self._reject(
                    scope, receive, send, 422, "Request body size exceeds API limit."
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, status: int, message: str
    ) -> None:
        correlation_id = str(uuid.uuid4())
        content = {"detail": message}
        if scope.get("path") == "/v1/pricing/scenarios":
            content = {
                "error_code": "VALIDATION_ERROR" if status == 422 else "INTERNAL_ERROR",
                "message": message,
                "correlation_id": correlation_id,
            }
        response = JSONResponse(
            status_code=status, content=content, headers={"X-Correlation-ID": correlation_id}
        )
        await response(scope, receive, send)

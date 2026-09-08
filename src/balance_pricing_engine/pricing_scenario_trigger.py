"""Auto-triggered pricing-scenario comparison for a detected competitor move.

Decision support only -- this module never changes a real price, and its
result is only ever surfaced (dashboard, alert enrichment, storage), never
fed back into a pricing decision automatically.

Reuses exactly the seam ``examples/run_market_intelligence_scenarios.py``
already established: ``pricing_intelligence.strategy.apply_market_strategy_to_config``
to build a competitor assumption from the current market, then the same
optimiser entry point every other scenario in the codebase goes through. No
optimiser logic is duplicated here.

Deliberately **not** part of the ``pricing_intelligence`` package: that
package is architecturally forbidden from importing the optimiser at all
(see ``tests/unit/test_pricing_intelligence_boundaries.py::
test_market_intelligence_does_not_import_the_optimiser`` -- "nothing in the
intelligence layer may price"). This module sits alongside it, the same way
``examples/run_market_intelligence_scenarios.py`` already does, reading
market data through ``pricing_intelligence.strategy`` (the one sanctioned
seam) and pricing through the optimiser -- so it is the caller
(``scripts/collect_market_rates_workflow.py``) that wires the two together
via ``orchestration.run_operational_cycle``'s injectable
``scenario_trigger_fn``, not ``pricing_intelligence`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    apply_market_strategy_to_config,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

#: (label, strategy, offset_bps). "Model optimum" is not a fifth strategy --
#: it is MARKET_MEDIAN's own result, relabelled: under a median competitor
#: assumption, the optimiser's recommended rate *is* the model's answer to
#: "what should we price". Matches the "Scenario E ... is Scenario B's
#: recommended rate" note in examples/run_market_intelligence_scenarios.py.
_SCENARIOS: tuple[tuple[str, CompetitorRateStrategy, float], ...] = (
    ("No change", CompetitorRateStrategy.MANUAL, 0.0),
    ("Match median", CompetitorRateStrategy.MARKET_MEDIAN, 0.0),
    ("Match leader", CompetitorRateStrategy.MARKET_LEADER, 0.0),
    ("Leader -5bps", CompetitorRateStrategy.MARKET_LEADER, -5.0),
)


@dataclass(frozen=True)
class ScenarioRow:
    """One row of the comparison. Rates are fractions (0.0489), matching
    ``EngineConfig``/``OptimizationResult`` scale -- format with
    ``reporting.fmt_rate`` or ``f"{value:.2%}"``, not ``.2f``."""

    label: str
    strategy: str
    competitor_rate: float
    our_rate: float
    objective: float


@dataclass(frozen=True)
class ScenarioComparison:
    """Result of :func:`run_triggered_scenarios` for one term."""

    term_months: int
    rows: tuple[ScenarioRow, ...] = field(default_factory=tuple)

    @property
    def model_optimum(self) -> "ScenarioRow | None":
        return next((row for row in self.rows if row.label == "Model optimum"), None)

    @property
    def current(self) -> "ScenarioRow | None":
        return next((row for row in self.rows if row.label == "No change"), None)

    def as_dicts(self) -> list[dict]:
        return [
            {
                "label": row.label,
                "strategy": row.strategy,
                "competitor_rate": row.competitor_rate,
                "our_rate": row.our_rate,
                "objective": row.objective,
            }
            for row in self.rows
        ]


def run_triggered_scenarios(
    base_config: EngineConfig,
    *,
    term_months: int,
    snapshot: MarketSnapshot,
    max_boundary_expansions: int = 2,
) -> ScenarioComparison:
    """Run the fixed scenario set for one term, against ``snapshot``.

    ``base_config`` supplies every economic assumption (elasticities, cost of
    funds, penalty terms) unchanged -- only the competitor-rate input differs
    per scenario, via ``apply_market_strategy_to_config``.
    """
    rows: list[ScenarioRow] = []
    model_optimum_source: ScenarioRow | None = None
    for label, strategy, offset_bps in _SCENARIOS:
        scenario_config = apply_market_strategy_to_config(
            base_config,
            strategy=strategy,
            snapshot=snapshot,
            term_months=term_months,
            offset_bps=offset_bps,
        )
        diagnostics = run_baseline_diagnostics(
            scenario_config, max_boundary_expansions=max_boundary_expansions
        )
        best = diagnostics.best_result
        row = ScenarioRow(
            label=label,
            strategy=strategy.value,
            competitor_rate=scenario_config.market.competitor_loan_rate,
            our_rate=best.loan_rate,
            objective=best.total_objective,
        )
        rows.append(row)
        if strategy is CompetitorRateStrategy.MARKET_MEDIAN and offset_bps == 0.0:
            model_optimum_source = row

    if model_optimum_source is not None:
        rows.append(
            ScenarioRow(
                label="Model optimum",
                strategy="MODEL_OPTIMUM",
                competitor_rate=model_optimum_source.competitor_rate,
                our_rate=model_optimum_source.our_rate,
                objective=model_optimum_source.objective,
            )
        )

    return ScenarioComparison(term_months=term_months, rows=tuple(rows))


def format_scenario_block(comparison: ScenarioComparison) -> str:
    """Compact enrichment block for a Telegram/webhook alert.

    Deliberately terse -- current/match-median/match-leader/model-optimum
    only, no full grid -- per "do not send large modelling tables through
    Telegram"; the full comparison is available via ``get_scenario_runs()``/
    the dashboard for anyone who wants it.
    """
    by_label = {row.label: row for row in comparison.rows}
    lines = ["PRICING ENGINE", ""]
    for label in ("No change", "Match median", "Match leader", "Model optimum"):
        row = by_label.get(label)
        if row is None:
            continue
        display_label = "Current rate" if label == "No change" else label
        lines.append(f"{display_label + ':':<18}{row.our_rate:.2%}")
    lines.append("")
    lines.append("Decision support only -- no automatic pricing action taken.")
    return "\n".join(lines)

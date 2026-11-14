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

from dataclasses import asdict, dataclass, field

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    apply_market_strategy_to_config,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.scenario_reporting import (
    MEDIAN_REFERENCE_LABEL,
    decision_quality_summary,
    diagnostic_metadata,
    strategy_label,
)

#: (label, strategy, offset_bps). The median-benchmark optimum is a reference
#: to MARKET_MEDIAN's own result, never a fifth optimisation strategy.
#: Matches the "Scenario E ... is Scenario B's recommended rate" note in
#: examples/run_market_intelligence_scenarios.py.
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
    # None preserves missing legacy evidence rather than implying a clean diagnostic.
    policy_band_breached: bool | None = None
    boundary_warning: bool | None = None
    initial_boundary_warning: bool | None = None
    expansion_capped: bool | None = None
    expansion_rounds: int | None = None
    funding_shortfall: float | None = None
    roe_constraint_relaxed: bool | None = None
    expected_loan_volume: float | None = None
    expected_deposit_volume: float | None = None

    @property
    def decision_quality_summary(self) -> str:
        return decision_quality_summary(self)


@dataclass(frozen=True)
class ScenarioComparison:
    """Result of :func:`run_triggered_scenarios` for one term."""

    term_months: int
    rows: tuple[ScenarioRow, ...] = field(default_factory=tuple)

    product_type: ProductType = ProductType.HOME_LOAN

    @property
    def median_reference(self) -> "ScenarioRow | None":
        return next((row for row in self.rows if row.label == "Match median"), None)

    @property
    def model_optimum(self) -> "ScenarioRow | None":
        # Backward-compatible accessor, including comparisons loaded from older storage.
        return self.median_reference or next(
            (row for row in self.rows if row.label == "Model optimum"), None
        )

    @property
    def current(self) -> "ScenarioRow | None":
        return next((row for row in self.rows if row.label == "No change"), None)

    def as_dicts(self) -> list[dict]:
        return [
            {**asdict(row), "decision_quality_summary": row.decision_quality_summary}
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

    ``snapshot.product_type`` selects which side of the config the scenario
    overlays: a ``TERM_DEPOSIT`` snapshot drives the deposit-side overlay
    (``deposit_strategy``/``deposit_snapshot``) and reports
    ``competitor_deposit_rate``/``best.deposit_rate``; any other snapshot
    (``HOME_LOAN``) drives the loan side as before. ``apply_market_strategy_to_config``
    requires the loan-side ``snapshot`` argument to be a home-loan market view for
    every non-MANUAL strategy, so a term-deposit snapshot must never be passed there.
    """
    is_deposit = ProductType(snapshot.product_type) is ProductType.TERM_DEPOSIT
    rows: list[ScenarioRow] = []
    for label, strategy, offset_bps in _SCENARIOS:
        if is_deposit:
            scenario_config = apply_market_strategy_to_config(
                base_config,
                deposit_strategy=strategy,
                deposit_snapshot=snapshot,
                deposit_term_months=term_months,
                deposit_offset_bps=offset_bps,
            )
        else:
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
        if is_deposit:
            competitor_rate = scenario_config.market.competitor_deposit_rate
            our_rate = best.deposit_rate
        else:
            competitor_rate = scenario_config.market.competitor_loan_rate
            our_rate = best.loan_rate
        row = ScenarioRow(
            label=label,
            strategy=strategy.value,
            competitor_rate=competitor_rate,
            our_rate=our_rate,
            objective=best.total_objective,
            **diagnostic_metadata(diagnostics),
        )
        rows.append(row)
    return ScenarioComparison(
        term_months=term_months, rows=tuple(rows), product_type=ProductType(snapshot.product_type)
    )


def format_scenario_block(comparison: ScenarioComparison) -> str:
    """Compact, product-explicit diagnostic summaries; never an approval or pricing action."""
    product = comparison.product_type
    title = "TERM DEPOSITS" if product == ProductType.TERM_DEPOSIT else "HOME LOANS"
    lines = [f"PRICING ENGINE — {title}", f"Term: {comparison.term_months} months", ""]
    for row in comparison.rows:
        if row.label in ("Model optimum", "Leader -5bps"):
            continue
        # "No change" is the optimiser's recommendation under an *unchanged*
        # competitor assumption -- it is not the bank's actual currently
        # advertised rate, which this module never reads. Labelling it
        # "Current rate" would present a recommendation as an existing price.
        label = "No-change assumption" if row.label == "No change" else row.label
        lines.append(f"{label} ({strategy_label(row.strategy, product)})")
        lines.append(f"Model-best rate: {row.our_rate:.2%}")
        lines.append(f"Status: {row.decision_quality_summary}")
    reference = comparison.model_optimum
    if reference is not None:
        lines.extend(
            [
                "",
                f"{MEDIAN_REFERENCE_LABEL}: {reference.our_rate:.2%}",
                "Reference is the Match median run, not another strategy.",
            ]
        )
    lines.extend(["", "Synthetic decision support only.", "No automatic pricing action."])
    return "\n".join(lines)

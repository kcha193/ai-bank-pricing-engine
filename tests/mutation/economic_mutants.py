"""Pytest plugin that injects one economic mutant at runtime (TEST-002, Phase 3c).

Load with ``-p economic_mutants`` (``tests/mutation`` on ``PYTHONPATH``) and select the mutant
with ``ECONOMIC_MUTANT=<name>``. The mutant replaces production callables in memory for the
whole test session, before test modules are imported. Production source files are never
modified, and the normal CI suite never loads this plugin.

Each mutant reproduces a defect class the economic-invariant suite must detect; see
``run_mutation_checks.py`` and ``docs/economic_invariant_testing.md``.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import sys
from typing import Any, Callable

_PACKAGE = "balance_pricing_engine"
_MODULES = (
    "pricing.profit",
    "pricing.balance_sheet",
    "synthetic.response",
    "optimization.grid_search",
    "optimization.segmented",
    "diagnostics",
    "scenarios.cohort_accounting",
    "scenarios.runner",
    "scenarios.multiperiod",
    "governance.gates",
)


def _module(name: str):
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _replace_everywhere(original: Any, replacement: Any) -> int:
    """Rebind every ``balance_pricing_engine`` module global that *is* ``original``."""
    count = 0
    for name, module in list(sys.modules.items()):
        if not name.startswith(_PACKAGE) or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                setattr(module, attr, replacement)
                count += 1
    if count == 0:
        raise RuntimeError(f"mutant target {original!r} is not bound anywhere")
    return count


# ------------------------------------------------------------------------ mutants


def roe_floor_ignored_silently() -> None:
    """OPT-001: ignore the ROE floor and never disclose the relaxation."""
    grid_search = _module("optimization.grid_search")
    original = grid_search.select_best_result_with_disclosure

    def mutant(grid_results, min_return_on_equity=None):
        selection = original(grid_results, min_return_on_equity=None)
        return dataclasses.replace(selection, roe_constraint_relaxed=False)

    _replace_everywhere(original, mutant)


def loan_search_drops_shortfall_penalty() -> None:
    """OPT-002: the segmented search scores candidates without the shortfall penalty (the
    reported result is still evaluated canonically, so only the choice is wrong)."""
    segmented = _module("optimization.segmented")
    original = segmented._Problem.evaluate

    def mutant(self, loan_totals, deposit_totals):
        saved = self.parameters
        self.parameters = dataclasses.replace(saved, funding_shortfall_penalty_rate=0.0)
        try:
            return original(self, loan_totals, deposit_totals)
        finally:
            self.parameters = saved

    segmented._Problem.evaluate = mutant


def segmented_search_drops_capital_charge() -> None:
    """OPT-002 variant: capital charge omitted from the segmented search objective."""
    segmented = _module("optimization.segmented")
    original = segmented._Problem.evaluate

    def mutant(self, loan_totals, deposit_totals):
        saved = self.parameters
        self.parameters = dataclasses.replace(saved, capital_hurdle_rate=0.0)
        try:
            return original(self, loan_totals, deposit_totals)
        finally:
            self.parameters = saved

    segmented._Problem.evaluate = mutant


def surviving_loans_earn_cash_rate() -> None:
    """FIN-002: surviving contracted loans repriced at the cash rate (reinvestment)."""
    cohorts = _module("scenarios.cohort_accounting")
    original = cohorts.evaluate_active_portfolio

    def mutant(active_cohorts, market, balance_sheet):
        repriced = [
            dataclasses.replace(
                p, cohort=dataclasses.replace(p.cohort, loan_rate=market.cash_rate)
            )
            if p.period > p.cohort.origination_period
            else p
            for p in active_cohorts
        ]
        return original(repriced, market, balance_sheet)

    _replace_everywhere(original, mutant)


def back_book_ecl_omitted() -> None:
    """FIN-002: surviving cohorts carry no expected credit loss."""
    cohorts = _module("scenarios.cohort_accounting")
    original = cohorts.ActiveCohort.credit_loss

    def mutant(self):
        if self.period > self.cohort.origination_period:
            return 0.0
        return original.fget(self)

    cohorts.ActiveCohort.credit_loss = property(mutant)


def portfolio_funding_penalty_omitted() -> None:
    """FIN-002: the active-book shortfall penalty is never charged."""
    cohorts = _module("scenarios.cohort_accounting")
    original = cohorts.calculate_funding_result

    def mutant(loan_volume, deposit_volume, ratio, penalty_rate):
        return original(loan_volume, deposit_volume, ratio, 0.0)

    cohorts.calculate_funding_result = mutant


def funding_summed_per_cohort() -> None:
    """FIN-002: funding terms computed per cohort and summed instead of on aggregates."""
    cohorts = _module("scenarios.cohort_accounting")
    original = cohorts.evaluate_active_portfolio
    economics_type = cohorts.PortfolioEconomics

    def mutant(active_cohorts, market, balance_sheet):
        parts = [original([p], market, balance_sheet) for p in active_cohorts]
        if not parts:
            return original(active_cohorts, market, balance_sheet)
        return economics_type(
            **{f.name: sum(getattr(part, f.name) for part in parts)
               for f in dataclasses.fields(economics_type)}
        )

    _replace_everywhere(original, mutant)


def survival_linear_not_geometric() -> None:
    """Runoff applied linearly (1 - r * t) instead of geometrically ((1 - r) ** t)."""
    cohorts = _module("scenarios.cohort_accounting")
    original = cohorts.survival_factor

    def mutant(runoff_rate, periods_elapsed):
        original(runoff_rate, periods_elapsed)  # keep input validation
        return max(0.0, 1.0 - runoff_rate * periods_elapsed)

    _replace_everywhere(original, mutant)


def policy_band_breach_reported_compliant() -> None:
    """A winner outside the original policy band is reported as compliant."""
    _module("diagnostics")._policy_band_breached = lambda *args, **kwargs: False
    _module("scenarios.runner")._segmented_policy_band_breached = (
        lambda *args, **kwargs: False
    )


def loan_competitor_gap_sign_flipped() -> None:
    """Response sign error: loan gap computed as competitor - offered."""
    response = _module("synthetic.response")
    original = response.loan_acceptance_probability

    def mutant(customers, offered_rate, competitor_rate, rate_elasticity,
               competitor_gap_elasticity, coefficients=None):
        mirrored = 2.0 * offered_rate - competitor_rate
        return original(customers, offered_rate, mirrored, rate_elasticity,
                        competitor_gap_elasticity, coefficients=coefficients)

    _replace_everywhere(original, mutant)


def wholesale_cost_not_floored() -> None:
    """Wholesale funding cost goes negative when deposits exceed loans."""
    profit = _module("pricing.profit")
    original = profit.expected_wholesale_funding_cost

    def mutant(loan_volume, deposit_volume, wholesale_funding_rate):
        return (loan_volume - deposit_volume) * wholesale_funding_rate

    _replace_everywhere(original, mutant)


def relaxed_roe_gate_passes() -> None:
    """Governance: the ROE gate passes even when the floor was relaxed."""
    gates = _module("governance.gates")
    original = gates._roe_floor_gate

    def mutant(config, diagnostics):
        result = original(config, diagnostics)
        return dataclasses.replace(result, status=gates.GateStatus.PASS)

    gates._roe_floor_gate = mutant


def negative_objective_gate_passes() -> None:
    """Governance: a loss-making optimum is not flagged by OBJECTIVE_POSITIVE."""
    gates = _module("governance.gates")
    original = gates._objective_positive_gate

    def mutant(diagnostics):
        return dataclasses.replace(original(diagnostics), status=gates.GateStatus.PASS)

    gates._objective_positive_gate = mutant


MUTANTS: dict[str, Callable[[], None]] = {
    fn.__name__: fn
    for fn in (
        roe_floor_ignored_silently,
        loan_search_drops_shortfall_penalty,
        segmented_search_drops_capital_charge,
        surviving_loans_earn_cash_rate,
        back_book_ecl_omitted,
        portfolio_funding_penalty_omitted,
        funding_summed_per_cohort,
        survival_linear_not_geometric,
        policy_band_breach_reported_compliant,
        loan_competitor_gap_sign_flipped,
        wholesale_cost_not_floored,
        relaxed_roe_gate_passes,
        negative_objective_gate_passes,
    )
}


def pytest_configure(config) -> None:
    name = os.environ.get("ECONOMIC_MUTANT", "")
    if not name:
        return
    if name not in MUTANTS:
        raise ValueError(f"Unknown ECONOMIC_MUTANT {name!r}; choose from {sorted(MUTANTS)}")
    for module in _MODULES:
        _module(module)
    MUTANTS[name]()


def pytest_report_header(config) -> str:
    return f"economic mutant: {os.environ.get('ECONOMIC_MUTANT') or 'none'}"

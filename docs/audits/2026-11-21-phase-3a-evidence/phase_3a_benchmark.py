"""Phase 3a solver timing on controlled synthetic tier candidates (no customer scoring).

    PYTHONPATH=src python docs/audits/2026-11-21-phase-3a-evidence/phase_3a_benchmark.py

Tier candidates come from a seeded concave demand curve, so timings measure the
portfolio search alone. Each case reports the best of three wall-clock runs.
"""

from __future__ import annotations

import json
import platform
import time

import numpy as np

from balance_pricing_engine.optimization.segmented import (
    DepositTierCandidates,
    LoanTierCandidates,
    PortfolioParameters,
    SolverSettings,
    solve_segmented_portfolio,
)

PARAMS = PortfolioParameters(
    required_deposit_funding_ratio=0.65,
    funding_shortfall_penalty_rate=0.018,
    loan_rwa_weight=0.35,
    capital_hurdle_rate=0.12,
    wholesale_funding_rate=0.037,
    cash_rate=0.025,
)


def _tiers(n_tiers: int, n_rates: int, loan: bool, seed: int):
    rng = np.random.default_rng(seed)
    tiers = []
    for t in range(n_tiers):
        rates = np.round(np.linspace(0.03, 0.09, n_rates) if loan else np.linspace(0.02, 0.06, n_rates), 6)
        scale = rng.uniform(1e8, 4e8)
        if loan:
            volume = scale / (1 + np.exp(120 * (rates - 0.055 - 0.002 * t)))
            tiers.append(LoanTierCandidates(f"l{t}", rates, volume, rates * volume, volume * 0.008))
        else:
            volume = scale / (1 + np.exp(-110 * (rates - 0.04 - 0.002 * t)))
            tiers.append(DepositTierCandidates(f"d{t}", rates, volume, rates * volume))
    return tiers


def _time(loans, deposits, settings):
    best = None
    for _ in range(3):
        start = time.perf_counter()
        solved = solve_segmented_portfolio(loans, deposits, PARAMS, settings=settings)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    d = solved.diagnostics
    return {
        "seconds": round(best, 4),
        "method": d.solver_method,
        "search_space_size": d.search_space_size,
        "candidate_evaluations": d.candidate_evaluations,
        "converged": d.converged,
        "iterations": d.iterations,
        "starts": d.starts,
        "objective": round(d.best_known_objective),
    }


def main() -> dict:
    exact = SolverSettings(method="exact")
    descent = SolverSettings(method="coordinate_descent")
    out = {"python": platform.python_version(), "machine": platform.machine(), "cases": {}}
    cases = {
        "1x1_121x81_exact": (1, 121, 1, 81, exact),
        "2x2_3_rates_exact": (2, 3, 2, 3, exact),
        "3x3_4_rates_exact": (3, 4, 3, 4, exact),
        "2x2_10_rates_exact": (2, 10, 2, 10, exact),
        "2x2_32_rates_exact": (2, 32, 2, 32, exact),
        "2x2_32_rates_descent": (2, 32, 2, 32, descent),
        "3x3_11_rates_exact": (3, 11, 3, 11, exact),
        "3x3_11_rates_descent": (3, 11, 3, 11, descent),
        "3x3_61_rates_descent": (3, 61, 3, 61, descent),
        "5x5_121_rates_descent": (5, 121, 5, 121, descent),
    }
    for name, (nl, rl, nd, rd, settings) in cases.items():
        out["cases"][name] = _time(_tiers(nl, rl, True, 1), _tiers(nd, rd, False, 2), settings)
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=1))

"""Compare pricing scenarios built on live NZ market intelligence.

Runs the existing optimiser once per competitor assumption so the scenarios are
directly comparable. The pricing model is not reimplemented here -- each
scenario only changes *how the competitor rate is sourced* and then calls
``run_baseline_diagnostics`` exactly as every other scenario does.

    Scenario A  No change            - the config's manual competitor rate
    Scenario B  Match market median  - MARKET_MEDIAN
    Scenario C  Match market leader  - MARKET_LEADER
    Scenario D  Leader minus 5 bps   - MARKET_LEADER with an offset
    Scenario E  Economically optimal - the optimiser's own answer under the
                                       market-median assumption, which is
                                       Scenario B's recommended rate

Requires a populated observation store. Every tracked bank now builds its
rate table client-side, so collection needs the optional browser extra:

    pip install -e '.[browser]' && python3 -m playwright install chromium
    python3 -m balance_pricing_engine.pricing_intelligence collect --render-javascript
"""

from __future__ import annotations

import argparse
from pathlib import Path

from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    apply_market_strategy_to_config,
)
from balance_pricing_engine.reporting import fmt_money, fmt_rate
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

SCENARIOS = (
    ("A", "No change (manual)", CompetitorRateStrategy.MANUAL, 0.0),
    ("B", "Match market median", CompetitorRateStrategy.MARKET_MEDIAN, 0.0),
    ("C", "Match market leader", CompetitorRateStrategy.MARKET_LEADER, 0.0),
    ("D", "Market leader -5 bps", CompetitorRateStrategy.MARKET_LEADER, -5.0),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Market-intelligence pricing scenarios")
    parser.add_argument("--config", default="configs/base.yaml")
    parser.add_argument("--term", type=int, default=12, help="Fixed term in months")
    parser.add_argument("--database", default=None, help="Observation store path")
    args = parser.parse_args()

    base_config = load_config(Path(args.config))
    # base_config.pricing_intelligence, not fresh defaults: --config's YAML
    # may configure a custom database path, bank selection, or freshness
    # limit, and silently ignoring it in favour of defaults would make this
    # script read a different store than the one --config actually points at.
    intelligence_config = base_config.pricing_intelligence or PricingIntelligenceConfig()
    if args.database:
        intelligence_config = intelligence_config.model_copy(
            update={"database_path": args.database}
        )

    with PricingIntelligenceService(
        config=intelligence_config,
        store=ObservationStore(intelligence_config.database_path),
    ) as service:
        snapshot = service.get_market_snapshot()
        if not snapshot.terms:
            raise SystemExit(
                "No market data. Run: python3 -m balance_pricing_engine."
                "pricing_intelligence collect --render-javascript "
                "(needs pip install -e '.[browser]' && "
                "python3 -m playwright install chromium)"
            )

        print("NZ Market Intelligence")
        print("=" * 78)
        print(snapshot.render())
        print()
        state = service.get_market_state()
        print(f"Market state: {state.state.value} - {state.rationale}")
        print()

        print(f"Pricing scenarios at {args.term} months")
        print("=" * 78)
        header = (
            f"{'':<3}{'Scenario':<24}{'Competitor':>12}{'Our rate':>11}"
            f"{'Objective':>16}{'vs A':>12}"
        )
        print(header)
        print("-" * len(header))

        baseline_objective = None
        for label, name, strategy, offset in SCENARIOS:
            config = apply_market_strategy_to_config(
                base_config,
                strategy=strategy,
                snapshot=snapshot,
                term_months=args.term,
                offset_bps=offset,
            )
            # The same optimiser every other scenario uses.
            diagnostics = run_baseline_diagnostics(config)
            best = diagnostics.best_result
            objective = best.total_objective
            if baseline_objective is None:
                baseline_objective = objective
            delta = objective - baseline_objective

            print(
                f"{label:<3}{name:<24}"
                f"{fmt_rate(config.market.competitor_loan_rate):>12}"
                f"{fmt_rate(best.loan_rate):>11}"
                f"{fmt_money(objective):>16}"
                f"{fmt_money(delta):>12}"
            )

        print()
        print(
            "Scenario E (economically optimal) is the optimiser's own recommendation "
            "under each assumption above -- the 'Our rate' column. The optimiser "
            "maximises risk-adjusted profit net of funding, ECL and the funding "
            "shortfall penalty; it does not simply match the market."
        )
        print()
        print(
            "Advertised rates are not negotiated customer rates. This output is "
            "decision support and does not replace internal pricing governance."
        )


if __name__ == "__main__":
    main()

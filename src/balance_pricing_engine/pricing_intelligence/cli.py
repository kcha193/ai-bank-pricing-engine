"""Command line interface for NZ pricing intelligence.

Mirrors the argparse subcommand style already used by
``balance_pricing_engine.competitor_rates.cli``.

    python3 -m balance_pricing_engine.pricing_intelligence collect
    python3 -m balance_pricing_engine.pricing_intelligence snapshot
    python3 -m balance_pricing_engine.pricing_intelligence changes --days 7
    python3 -m balance_pricing_engine.pricing_intelligence history --bank ANZ --term 12
    python3 -m balance_pricing_engine.pricing_intelligence market-state
    python3 -m balance_pricing_engine.pricing_intelligence position --rate 4.95 --term 12
    python3 -m balance_pricing_engine.pricing_intelligence strategy --strategy MARKET_MEDIAN
"""

from __future__ import annotations

import argparse
import logging
from typing import Optional

from balance_pricing_engine.config import load_config
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    ConsoleAlertSink,
    dispatch_alerts,
    format_alerts,
)
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import CompetitorRateStrategy


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = PricingIntelligenceConfig()
    if args.config:
        # configs/base.yaml's pricing_intelligence block would otherwise be
        # silently ignored -- this CLI previously always ran on defaults
        # regardless of what was configured there.
        engine_config = load_config(args.config)
        if engine_config.pricing_intelligence is not None:
            config = engine_config.pricing_intelligence
    if args.database:
        config = config.model_copy(update={"database_path": args.database})

    try:
        with PricingIntelligenceService(
            config=config,
            store=ObservationStore(config.database_path),
            render_javascript=getattr(args, "render_javascript", False),
        ) as service:
            handlers = {
                "collect": _collect,
                "snapshot": _snapshot,
                "changes": _changes,
                "history": _history,
                "market-state": _market_state,
                "position": _position,
                "strategy": _strategy,
            }
            handler = handlers.get(args.command)
            if handler is None:
                parser.print_help()
                return 1
            return handler(service, args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {exc}")
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pricing-intelligence",
        description="Monitor and interpret publicly advertised NZ retail bank pricing",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to an EngineConfig YAML (e.g. configs/base.yaml) to load "
            "pricing_intelligence settings from; --database still overrides "
            "just the database path"
        ),
    )
    parser.add_argument("--database", default=None, help="Path to the observation store")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--product",
        default=ProductType.HOME_LOAN.value,
        choices=[ProductType.HOME_LOAN.value, ProductType.TERM_DEPOSIT.value],
        help="Which market to act on (default: home_loan)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect", help="Fetch public bank pages, validate, store, and detect changes"
    )
    collect.add_argument(
        "--no-persist", action="store_true", help="Run without writing to the store"
    )
    collect.add_argument(
        "--no-snapshot", action="store_true", help="Do not save raw HTML snapshots"
    )
    collect.add_argument("--alerts", action="store_true", help="Print alerts for changes")
    collect.add_argument(
        "--render-javascript",
        action="store_true",
        help=(
            "Render pages in a headless browser before parsing. Currently required "
            "for live collection: every tracked bank builds its rate table "
            "client-side. Needs the optional 'browser' extra."
        ),
    )

    snapshot = subparsers.add_parser("snapshot", help="Show the current advertised market")
    snapshot.add_argument("--term", type=int, default=None, help="Detail one term, in months")
    snapshot.add_argument("--category", default="special", help="Rate category to compare")

    changes = subparsers.add_parser("changes", help="Show detected market changes")
    changes.add_argument("--days", type=int, default=7)
    changes.add_argument("--bank", default=None)
    changes.add_argument("--term", type=int, default=None)
    changes.add_argument("--alerts", action="store_true", help="Render as formatted alerts")

    history = subparsers.add_parser("history", help="Show advertised rate history")
    history.add_argument("--bank", default=None)
    history.add_argument("--term", type=int, default=None)
    history.add_argument("--days", type=int, default=None)
    history.add_argument("--category", default=None)

    subparsers.add_parser("market-state", help="Classify current market activity")

    position = subparsers.add_parser(
        "position", help="Position a rate against the advertised market"
    )
    position.add_argument("--rate", type=float, required=True, help="Our rate, in percent")
    position.add_argument("--term", type=int, default=12)

    strategy = subparsers.add_parser(
        "strategy", help="Resolve the competitor assumption for the pricing engine"
    )
    strategy.add_argument(
        "--strategy",
        default=CompetitorRateStrategy.MARKET_MEDIAN.value,
        choices=[member.value for member in CompetitorRateStrategy],
    )
    strategy.add_argument("--term", type=int, default=12)
    strategy.add_argument("--offset-bps", type=float, default=0.0)
    strategy.add_argument(
        "--manual-rate", type=float, default=None, help="Decimal rate for MANUAL"
    )

    return parser


def _product(args: argparse.Namespace) -> ProductType:
    return ProductType(getattr(args, "product", ProductType.HOME_LOAN.value))


def _default_category(args: argparse.Namespace) -> str:
    """Comparable category for the selected market.

    Mortgages are compared on advertised specials (where front-book competition
    happens); deposits on the plain term-deposit tier, excluding PIE variants.
    """
    supplied = getattr(args, "category", None)
    if supplied not in (None, "special"):
        return supplied
    if _product(args) is ProductType.TERM_DEPOSIT:
        return "term_deposit"
    return "special"


def _collect(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    outcome = service.collect(
        persist=not args.no_persist,
        persist_snapshot=not args.no_snapshot,
        product_type=_product(args),
    )
    print(outcome.render())

    if outcome.validation.issues:
        print("\nValidation issues:")
        for issue in outcome.validation.issues:
            print(f"  {issue}")

    if outcome.changes:
        print("\nDetected changes:")
        for change in outcome.changes:
            flag = "  [SUSPICIOUS]" if change.suspicious else ""
            print(f"  {change.change_type.value:<18} {change.describe()}{flag}")

    if args.alerts and outcome.changes:
        print()
        if args.no_persist:
            # A dry run never writes the accepted observations, so the live
            # snapshot still describes the unchanged current state, not what
            # these prospective changes propose. Showing it as market
            # context (median, rank movement) would misrepresent the
            # preview; omit it rather than compare against stale data.
            snapshot, previous_snapshot = None, None
        else:
            snapshot = service.get_market_snapshot(
                product_type=_product(args), rate_category=_default_category(args)
            )
            previous_snapshot = service.get_previous_market_snapshot(
                product_type=_product(args), rate_category=_default_category(args)
            )
        dispatch_alerts(
            format_alerts(
                [change for change in outcome.changes if change.is_rate_move],
                snapshot,
                previous_snapshot,
            ),
            ConsoleAlertSink(),
        )

    # A run where every bank failed to fetch is a failure, and so is one where
    # pages parsed but validation rejected every record -- either way nothing
    # usable was stored, and a scheduler must not treat that as success.
    if not outcome.report.any_success or outcome.rate_count == 0:
        return 1
    return 0


def _snapshot(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    snapshot = service.get_market_snapshot(
        product_type=_product(args), rate_category=_default_category(args)
    )
    if not snapshot.terms:
        print("No market data. Run 'collect' first.")
        return 1

    if args.term is None:
        label = "TERM DEPOSIT" if _product(args) is ProductType.TERM_DEPOSIT else "MORTGAGE"
        print(f"NZ {label} MARKET ({_default_category(args)}) as at {snapshot.as_of:%d %b %Y}")
        print()
        print(snapshot.render())
        return 0

    market = snapshot.term(args.term)
    if market is None:
        print(f"No data at {args.term} months. Available: {list(snapshot.term_months)}")
        return 1
    print(f"{args.term} MONTH FIXED ({_default_category(args)})")
    print()
    for entry in market.ranking():
        print(f"  {entry.bank:<12}{entry.rate_pct:>6.2f}   #{entry.rank}")
    print()
    print(f"  Best:   {market.best_rate:.2f}  ({', '.join(market.market_leaders)})")
    print(f"  Median: {market.median_rate:.2f}")
    print(f"  Mean:   {market.mean_rate:.2f}")
    worst_rate = (
        market.highest_rate
        if market.product_type is ProductType.HOME_LOAN
        else market.lowest_rate
    )
    print(f"  Worst:  {worst_rate:.2f}")
    print(f"  Spread: {market.spread_bps:.0f} bps across {market.bank_count} banks")
    return 0


def _changes(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    changes = service.get_changes(
        days=args.days,
        bank=args.bank,
        term_months=args.term,
        product_type=_product(args),
        rate_category=_default_category(args),
    )
    if not changes:
        print(f"No changes detected in the last {args.days} days.")
        return 0

    if args.alerts:
        dispatch_alerts(
            format_alerts(
                changes,
                service.get_market_snapshot(
                    product_type=_product(args), rate_category=_default_category(args)
                ),
            ),
            ConsoleAlertSink(),
        )
        return 0

    print(f"MARKET CHANGES - last {args.days} days ({len(changes)} events)")
    print()
    for change in changes:
        flag = "  [SUSPICIOUS]" if change.suspicious else ""
        print(f"  {change.detected_at:%d %b}  {change.describe()}{flag}")
    return 0


def _history(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    records = service.get_rate_history(
        bank=args.bank,
        term_months=args.term,
        product_type=_product(args),
        rate_category=_default_category(args),
        days=args.days,
    )
    if not records:
        print("No history for those filters.")
        return 1
    print(f"RATE HISTORY  bank={args.bank or 'all'}  term={args.term or 'all'}")
    print()
    for record in records:
        term = (
            f"{record.term_months_normalised}m"
            if record.term_months_normalised is not None
            else record.term_label_raw
        )
        print(
            f"  {record.captured_at_utc:%Y-%m-%d}  {record.bank:<10}{term:<6}"
            f"{record.rate_category:<10}{record.advertised_rate_pct:>6.2f}%"
        )
    return 0


def _market_state(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    assessment = service.get_market_state(
        product_type=_product(args), rate_category=_default_category(args)
    )
    payload = assessment.as_dict()
    print(f"MARKET STATE: {payload['state']}")
    print()
    for key in (
        "changes_7d",
        "banks_changed_7d",
        "terms_changed_7d",
        "mean_change_bps",
        "dominant_direction",
    ):
        print(f"  {key:<20}{payload[key]}")
    print()
    print(f"  {payload['rationale']}")
    return 0


def _position(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    position = service.get_competitive_position(
        our_rate=args.rate,
        term_months=args.term,
        product_type=_product(args),
        rate_category=_default_category(args),
    )
    print(f"COMPETITIVE POSITION  {args.rate:.2f}% at {args.term} months")
    print()
    for key, value in position.as_features().items():
        formatted = f"{value:.2f}" if isinstance(value, float) else str(value)
        print(f"  {key:<28}{formatted}")
    return 0


def _strategy(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    assumption = service.resolve_competitor_assumption(
        strategy=args.strategy,
        term_months=args.term,
        manual_rate=args.manual_rate,
        offset_bps=args.offset_bps,
        product_type=_product(args),
        rate_category=_default_category(args),
    )
    print("COMPETITOR ASSUMPTION FOR THE PRICING ENGINE")
    print()
    print(f"  strategy              {assumption.strategy.value}")
    print(f"  term_months           {assumption.term_months}")
    print(f"  advertised rate       {assumption.rate_pct:.3f}%")
    rate_label = (
        "competitor_deposit_rate"
        if _product(args) is ProductType.TERM_DEPOSIT
        else "competitor_loan_rate"
    )
    print(f"  {rate_label:<22}{assumption.competitor_loan_rate}")
    print(f"  offset_bps            {assumption.offset_bps:+.0f}")
    print(f"  source banks          {', '.join(assumption.source_banks) or 'n/a'}")
    print(f"  rationale             {assumption.rationale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

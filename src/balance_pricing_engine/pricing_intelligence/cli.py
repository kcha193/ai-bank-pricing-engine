"""Command line interface for NZ pricing intelligence.

Mirrors the argparse subcommand style already used by
``balance_pricing_engine.competitor_rates.cli``.

    python3 -m balance_pricing_engine.pricing_intelligence run
    python3 -m balance_pricing_engine.pricing_intelligence collect
    python3 -m balance_pricing_engine.pricing_intelligence snapshot
    python3 -m balance_pricing_engine.pricing_intelligence changes --days 7
    python3 -m balance_pricing_engine.pricing_intelligence history --bank ANZ --term 12
    python3 -m balance_pricing_engine.pricing_intelligence market-state
    python3 -m balance_pricing_engine.pricing_intelligence brief --weekly
    python3 -m balance_pricing_engine.pricing_intelligence position --rate 4.95 --term 12
    python3 -m balance_pricing_engine.pricing_intelligence strategy --strategy MARKET_MEDIAN
    python3 -m balance_pricing_engine.pricing_intelligence health
    python3 -m balance_pricing_engine.pricing_intelligence telegram-test
    python3 -m balance_pricing_engine.pricing_intelligence backup --destination ~/pi-backups
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
from typing import Optional

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.config import load_config
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    ConsoleAlertSink,
    build_telegram_sink_from_env,
    build_webhook_sink_from_env,
    dispatch_alerts,
    dispatch_alerts_safely,
    format_alerts,
    plain_alert,
)
from balance_pricing_engine.pricing_intelligence.briefing import build_daily_brief, build_weekly_brief
from balance_pricing_engine.pricing_intelligence.config import (
    PricingIntelligenceConfig,
    resolve_database_path_from_env,
)
from balance_pricing_engine.pricing_intelligence.locking import AlreadyRunningError, SingleInstanceLock
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_ALREADY_RUNNING,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.rbnz import fetch_ocr_series
from balance_pricing_engine.pricing_intelligence.strategy import (
    CashRateStrategy,
    CompetitorRateStrategy,
    resolve_cash_rate,
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = resolve_database_path_from_env(PricingIntelligenceConfig())
        if args.config:
            # configs/base.yaml's pricing_intelligence block would otherwise be
            # silently ignored -- this CLI previously always ran on defaults
            # regardless of what was configured there.
            engine_config = load_config(args.config)
            if engine_config.pricing_intelligence is not None:
                config = engine_config.pricing_intelligence
        if args.database:
            config = config.model_copy(update={"database_path": args.database})

        if args.command == "ocr":
            # Store-independent (see _ocr's docstring): dispatch before the
            # ObservationStore/PricingIntelligenceService context below is
            # entered, so a read-only deployment or an unavailable
            # --database path can't block this command, and a successful
            # invocation doesn't needlessly create/migrate a SQLite database
            # it will never touch. Still store-dependent config (timeout,
            # retries, backoff, user agent) that --config loaded above,
            # though -- store independence isn't fetch-configuration
            # independence.
            return _ocr(config.collection)

        if args.command == "run":
            # Takes its own single-instance lock (see locking.py) before
            # entering the service/store context below, the same as
            # scripts/collect_market_rates_workflow.py's scheduled entry
            # point -- an interactive `run` and a scheduled one must not be
            # able to interleave against the same database either.
            return _run(config, args)

        if args.command == "telegram-test":
            # Store-independent, same rationale as "ocr" above: a delivery
            # smoke test needs no database at all.
            return _telegram_test()

        if args.command == "backup":
            # backup_database() opens config.database_path directly via
            # sqlite3 -- no need to construct an ObservationStore/service
            # first just to hand it a path.
            return _backup(config, args)

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
                "brief": _brief,
                "health": _health,
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


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("must be finite")
    return result


def _nonnegative_rate(value: str) -> float:
    result = _finite_float(value)
    if result < 0:
        raise argparse.ArgumentTypeError("rate must be non-negative")
    return result


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
    snapshot.add_argument("--term", type=_positive_int, default=None, help="Detail one term, in months")
    snapshot.add_argument(
        "--category",
        default=None,
        help="Rate category to compare (default: 'special' for home loans, "
        "'term_deposit' for term deposits)",
    )

    changes = subparsers.add_parser("changes", help="Show detected market changes")
    changes.add_argument("--days", type=_positive_int, default=7)
    changes.add_argument("--bank", default=None)
    changes.add_argument("--term", type=_positive_int, default=None)
    changes.add_argument("--alerts", action="store_true", help="Render as formatted alerts")
    changes.add_argument(
        "--category",
        default=None,
        help=(
            "Rate category to filter to (default: 'special' for home loans, "
            "'term_deposit' for term deposits). Pass '--category all' to see "
            "every stored category, e.g. a mortgage 'standard' repricing."
        ),
    )

    history = subparsers.add_parser("history", help="Show advertised rate history")
    history.add_argument("--bank", default=None)
    history.add_argument("--term", type=_positive_int, default=None)
    history.add_argument("--days", type=_positive_int, default=None)
    history.add_argument("--category", default=None)

    subparsers.add_parser("market-state", help="Classify current market activity")

    brief = subparsers.add_parser(
        "brief", help="Generate the daily or weekly intelligence brief"
    )
    brief.add_argument(
        "--weekly", action="store_true", help="Generate the weekly summary instead of daily"
    )
    brief.add_argument(
        "--send",
        action="store_true",
        help="Deliver via configured webhook/Telegram, in addition to printing it",
    )

    position = subparsers.add_parser(
        "position", help="Position a rate against the advertised market"
    )
    position.add_argument("--rate", type=_nonnegative_rate, required=True, help="Our rate, in percent")
    position.add_argument("--term", type=_positive_int, default=12)

    strategy = subparsers.add_parser(
        "strategy", help="Resolve the competitor assumption for the pricing engine"
    )
    strategy.add_argument(
        "--strategy",
        default=CompetitorRateStrategy.MARKET_MEDIAN.value,
        choices=[member.value for member in CompetitorRateStrategy],
    )
    strategy.add_argument("--term", type=_positive_int, default=12)
    strategy.add_argument("--offset-bps", type=_finite_float, default=0.0)
    strategy.add_argument(
        "--manual-rate", type=_nonnegative_rate, default=None, help="Decimal rate for MANUAL"
    )

    run = subparsers.add_parser(
        "run",
        help=(
            "Full operational cycle: collect, validate, persist, detect changes, "
            "classify market state, update collector health, and dispatch alerts "
            "(webhook/Telegram, whichever is configured) in one command"
        ),
    )
    run.add_argument(
        "--render-javascript",
        action="store_true",
        help="Render pages in a headless browser. Required for live collection.",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Collect, validate, and classify as normal, but persist nothing and "
            "send no real alerts -- every alert is printed to the console instead. "
            "Safe to run against a real Telegram/webhook configuration."
        ),
    )
    run.add_argument(
        "--lock-file",
        default="data/processed/pricing_intelligence/collection.lock",
        help="Single-instance lock path (shared with the scheduled workflow if reused)",
    )

    subparsers.add_parser(
        "ocr",
        help=(
            "Show the current RBNZ Official Cash Rate and recent trend "
            "(a separate data source from the bank collectors above)"
        ),
    )

    subparsers.add_parser(
        "health",
        help="Database, collector, and alert-channel status for troubleshooting",
    )

    subparsers.add_parser(
        "telegram-test",
        help="Send a fixed test message to the configured Telegram bot/chat",
    )

    backup = subparsers.add_parser(
        "backup", help="Write a timestamped backup of the observation store"
    )
    backup.add_argument(
        "--destination", required=True, help="Directory to write timestamped backups into"
    )
    backup.add_argument(
        "--keep", type=_positive_int, default=30, help="How many recent backups to retain"
    )

    return parser


def _product(args: argparse.Namespace) -> ProductType:
    return ProductType(getattr(args, "product", ProductType.HOME_LOAN.value))


def _default_category(args: argparse.Namespace) -> str:
    """Comparable category for the selected market.

    Mortgages are compared on advertised specials (where front-book competition
    happens); deposits on the plain term-deposit tier, excluding PIE variants.

    ``None`` is the only sentinel for "not supplied": both ``snapshot`` and
    ``history`` default ``--category`` to ``None``, so an explicit
    ``--category special`` is honoured as-is rather than being mistaken for
    the omitted case and rewritten to the other product's default.
    """
    supplied = getattr(args, "category", None)
    if supplied is not None:
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
    # usable was stored, and a scheduler must not treat that as success. Not
    # sufficient on its own, though: a term-deposit run can validation-accept
    # only non_comparable audit-only rows, or a real but off-default category
    # like pie_term_deposit, while missing every ordinary term_deposit row --
    # rate_count is positive but the default market snapshot this same
    # invocation just queried above is empty. Mirrors
    # scripts/collect_market_rates_workflow.py's _comparable_rate_count().
    comparable_category = _default_category(args)
    comparable_count = sum(
        1 for record in outcome.validation.accepted if record.rate_category == comparable_category
    )
    if not outcome.report.any_success or outcome.rate_count == 0 or comparable_count == 0:
        return 1
    return 0


def _run(
    config: PricingIntelligenceConfig, args: argparse.Namespace
) -> int:
    """Interactive entry point for the full operational cycle.

    Shares :func:`~.orchestration.run_operational_cycle` with
    ``scripts/collect_market_rates_workflow.py``'s scheduled entry point, so
    a manual run and a scheduled one classify and alert identically -- see
    that module's docstring.

    Does not auto-trigger pricing scenarios -- that needs a
    scenario_trigger_fn built from a module that imports the optimiser, which
    this package (including this CLI) is architecturally forbidden from doing
    (see ``tests/unit/test_pricing_intelligence_boundaries.py``). Use the
    scheduled workflow script for scenario-triggered runs.
    """
    try:
        with SingleInstanceLock(Path(args.lock_file)):
            with PricingIntelligenceService(
                config=config,
                store=ObservationStore(config.database_path),
                render_javascript=args.render_javascript,
            ) as service:
                result = run_operational_cycle(
                    service, product_type=_product(args), dry_run=args.dry_run
                )
    except AlreadyRunningError as exc:
        print(f"Skipped: {exc}")
        return EXIT_ALREADY_RUNNING

    print(result.outcome.render())
    if result.outcome.changes:
        print("\nDetected changes:")
        for change in result.outcome.changes:
            flag = "  [SUSPICIOUS]" if change.suspicious else ""
            print(f"  {change.change_type.value:<18} {change.describe()}{flag}")
    print()
    print(f"Status:      {result.status}")
    print(f"Alerts sent: {result.alerts_sent}")
    if result.run_record is not None:
        print(f"Run id:      {result.run_record.run_id}")
    if args.dry_run:
        print()
        print("Dry run: nothing persisted, no real alerts sent.")
    return result.code


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
    # Unlike snapshot/history, "changes" lists discrete events rather than a
    # ranked comparison table, so unlike _default_category()'s other callers
    # it also accepts "all" to mean no category filter at all (get_changes()
    # already treats rate_category=None that way) -- without this, a
    # non-default category such as a mortgage "standard" repricing or a
    # deposit "special" was unreachable through this command.
    rate_category = None if args.category == "all" else _default_category(args)
    changes = service.get_changes(
        days=args.days,
        bank=args.bank,
        term_months=args.term,
        product_type=_product(args),
        rate_category=rate_category,
    )
    if not changes:
        print(f"No changes detected in the last {args.days} days.")
        return 0

    if args.alerts:
        if rate_category is None:
            # "all" categories: format_alert()'s market-context guard
            # (snapshot.rate_category == change.rate_category) can never
            # match a single unfiltered (rate_category=None) snapshot --
            # None never equals a change's own concrete category -- which
            # would silently drop market-median/rank context from every
            # alert. Build one concrete-category snapshot per distinct
            # category actually present among these changes instead, so
            # each group of changes gets a snapshot the guard can match.
            alerts = [
                alert
                for category in sorted({change.rate_category for change in changes})
                for alert in format_alerts(
                    [change for change in changes if change.rate_category == category],
                    service.get_market_snapshot(product_type=_product(args), rate_category=category),
                )
            ]
            alerts.sort(key=lambda alert: (-abs(alert.change.change_bps or 0), alert.change.bank))
        else:
            alerts = format_alerts(
                changes,
                service.get_market_snapshot(
                    product_type=_product(args), rate_category=rate_category
                ),
            )
        dispatch_alerts(alerts, ConsoleAlertSink())
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
    if not service.get_market_snapshot(product_type=_product(args)).terms:
        print("No fresh market data; market activity is unknown.")
        return 1
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


def _brief(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    """Generate the daily or weekly intelligence brief.

    Deliberately a separate command from ``run``, not folded into
    ``orchestration.run_operational_cycle`` -- the project's own
    intelligence/pricing/alerting split (see docs/pricing_intelligence.md)
    treats "what needs my attention" as a distinct concern from collection
    itself, and an operator who wants both chains them in one schedule line
    (``pricing-intelligence run && pricing-intelligence brief --send``; see
    the Operations section of the docs).
    """
    product = _product(args)
    category = _default_category(args)
    snapshot = service.get_market_snapshot(product_type=product, rate_category=category)
    if not snapshot.terms:
        print("No market data. Run 'run' or 'collect' first.")
        return 1

    assessment = service.get_market_state(product_type=product, rate_category=category)
    now = datetime.now(timezone.utc)
    lookback_days = service.config.market_state.lookback_days

    if args.weekly:
        week_changes = service.get_changes(
            days=lookback_days, product_type=product, rate_category=category
        )
        cutoff = now - timedelta(days=lookback_days)
        run_records = [
            record
            for record in service.store.get_recent_run_records(product_type=product, limit=200)
            if record.started_at >= cutoff
        ]
        text = build_weekly_brief(
            as_of=now,
            week_changes=week_changes,
            week_assessment=assessment,
            snapshot=snapshot,
            run_records=run_records,
        )
    else:
        today_changes = service.get_changes(days=1, product_type=product, rate_category=category)
        health = service.store.get_collector_health(product_type=product)
        text = build_daily_brief(
            as_of=now,
            today_changes=today_changes,
            week_assessment=assessment,
            snapshot=snapshot,
            collector_health=health,
            tracked_banks=service.config.selected_banks,
        )

    print(text)

    if args.send:
        title = "NZ Pricing Intelligence -- Weekly Brief" if args.weekly else "NZ Pricing Intelligence -- Daily Brief"
        alert = plain_alert(title, text)
        sinks = [
            sink
            for sink in (build_webhook_sink_from_env(), build_telegram_sink_from_env())
            if sink is not None
        ]
        if not sinks:
            print("\n(--send requested, but no webhook or Telegram channel is configured)")
        else:
            sent = sum(dispatch_alerts_safely([alert], sink) for sink in sinks)
            print(f"\nDelivered to {sent}/{len(sinks)} configured channel(s).")
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


def _ocr(collection_config: CollectionConfig) -> int:
    """Print the current RBNZ OCR and recent trend.

    A separate data source from the bank collectors: RBNZ's own published
    series already carries its full monthly history, so unlike ``collect``
    for bank rates, this reads it fresh each call rather than through the
    observation store. Dispatched from ``main()`` before the
    ObservationStore/PricingIntelligenceService context is entered -- this
    command needs neither the store nor the service, but it still needs the
    ``--config``-loaded fetch settings (timeout, retries, backoff, user
    agent): store independence is not fetch-configuration independence.

    Runs the series through the same currency check
    ``resolve_cash_rate(RBNZ_OCR)`` applies before the pricing overlay would
    accept it (rejects a series that's gone unrefreshed longer than an OCR
    review cycle, or one with a future-dated newest observation) -- this
    command must not report a rate as "the current OCR" that the pricing
    seam would refuse to price off.
    """
    series = fetch_ocr_series(config=collection_config)
    resolved = resolve_cash_rate(CashRateStrategy.RBNZ_OCR, ocr_series=series)
    current = series.current
    print("RBNZ OFFICIAL CASH RATE")
    print()
    print(f"  OCR                    {resolved.rate_pct:.2f}%")
    if current.bank_bill_90d_pct is not None:
        print(f"  90-day bank bill rate  {current.bank_bill_90d_pct:.2f}%")
    print(f"  as at                  {resolved.as_of_period}")
    for months in (1, 3, 12):
        change = series.change_bps(months)
        label = "month" if months == 1 else "months"
        change_text = f"{change:+.0f} bps" if change is not None else "n/a"
        print(f"  change over {months} {label:<7}{change_text}")
    print(f"  source                 {series.source_url}")
    return 0


def _health(service: PricingIntelligenceService, args: argparse.Namespace) -> int:
    """Database, collector, and alert-channel status -- the first stop for troubleshooting."""
    product = _product(args)
    print("PRICING INTELLIGENCE HEALTH")
    print()

    report = service.store.check_integrity()
    print(f"Database: {'OK' if report.ok else 'ISSUES DETECTED'}")
    for issue in report.issues:
        print(f"  - {issue}")
    print()

    latest_run = service.store.get_latest_run_record(product_type=product)
    if latest_run is None:
        print("Last run: none recorded yet (run 'run' to start collecting)")
    else:
        print(f"Last run: {latest_run.started_at:%d %b %Y %H:%M} UTC -- {latest_run.status}")
    print()

    health_by_bank = {h.bank: h for h in service.store.get_collector_health(product_type=product)}
    for bank in service.config.selected_banks:
        health = health_by_bank.get(bank)
        if health is None:
            print(f"  {bank:<12}{'UNKNOWN':<16}no collection recorded yet")
            continue
        status = "FRESH" if health.consecutive_failures == 0 else f"FAILING ({health.consecutive_failures}x)"
        last_seen = (
            health.last_success_at.strftime("%d %b %H:%M") if health.last_success_at else "never"
        )
        print(f"  {bank:<12}{status:<16}last seen {last_seen}")
    print()

    telegram_configured = build_telegram_sink_from_env() is not None
    webhook_configured = build_webhook_sink_from_env() is not None
    print(f"Telegram: {'configured' if telegram_configured else 'not configured'}")
    print(f"Webhook:  {'configured' if webhook_configured else 'not configured'}")
    print()

    snapshot = service.get_market_snapshot(product_type=product)
    print(f"Latest market snapshot: {'VALID' if snapshot.terms else 'EMPTY'}")

    return 0 if report.ok else 1


def _telegram_test() -> int:
    """Send a fixed test message via the configured Telegram bot/chat.

    Store-independent (see the dispatch comment in ``main()``) -- a delivery
    smoke test should not require a database at all, and should be safe to
    run before any collection has happened.
    """
    sink = build_telegram_sink_from_env()
    if sink is None:
        print(
            "Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "(see .env.example)."
        )
        return 1
    try:
        sink.send(plain_alert("NZ Pricing Intelligence Telegram test successful.", ""))
    except Exception as exc:  # noqa: BLE001 - reported at the CLI boundary
        print(f"Telegram test failed: {exc}")
        return 1
    print("Sent: ✅ NZ Pricing Intelligence Telegram test successful.")
    return 0


def _backup(config: PricingIntelligenceConfig, args: argparse.Namespace) -> int:
    """Write a timestamped backup of the observation store and prune old ones."""
    from balance_pricing_engine.pricing_intelligence.backup import backup_database

    if not Path(config.database_path).exists():
        print(f"No database at {config.database_path}; nothing to back up.")
        return 1
    backup_path = backup_database(config.database_path, args.destination, keep=args.keep)
    print(f"Backup written to {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

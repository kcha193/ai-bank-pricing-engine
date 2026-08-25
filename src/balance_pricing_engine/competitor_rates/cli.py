"""Command line interface for competitor-rate ingestion and application."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import uuid

import yaml

from balance_pricing_engine.competitor_rates.apply import (
    apply_competitor_rates_to_config,
    load_and_resolve_competitor_rates,
)
from balance_pricing_engine.competitor_rates.collectors import (
    COLLECTORS_BY_BANK,
    BankPricingCollector,
)
from balance_pricing_engine.competitor_rates.extractors import extract_all_sources
from balance_pricing_engine.competitor_rates.interest_co_nz import fetch_interest_co_nz_rates
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    CompetitorRateRun,
    CompetitorRatesConfig,
    ProductType,
    ValidationSummary,
)
from balance_pricing_engine.competitor_rates.sources import OFFICIAL_SOURCES
from balance_pricing_engine.competitor_rates.store import load_rate_run, write_rate_run
from balance_pricing_engine.competitor_rates.validate import validate_rate_run, validate_staleness
from balance_pricing_engine.config import load_config


DEFAULT_OUTPUT_DIR = Path("data/processed/competitor_rates")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            return _fetch(args)
        if args.command == "fetch-interest-co-nz":
            return _fetch_interest_co_nz(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "apply":
            return _apply(args)
        if args.command == "report":
            return _report(args)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Competitor-rate ingestion workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="Fetch official bank pages and write artifacts")
    fetch.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    fetch.add_argument("--raw-dir", default="data/raw/competitor_rates")
    fetch.add_argument("--strict", default="true", choices=["true", "false"])
    fetch.add_argument(
        "--browser-fallback",
        action="store_true",
        help="Render pages with Playwright if static HTML extraction finds no rates",
    )

    interest = subparsers.add_parser(
        "fetch-interest-co-nz",
        help="Fetch fallback competitor rates from interest.co.nz aggregator tables",
    )
    interest.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    interest.add_argument("--raw-dir", default="data/raw/competitor_rates")
    interest.add_argument("--strict", default="false", choices=["true", "false"])

    validate = subparsers.add_parser("validate", help="Validate a stored competitor-rate file")
    validate.add_argument("--source-file", default=str(DEFAULT_OUTPUT_DIR / "latest.json"))
    validate.add_argument("--strict", default="true", choices=["true", "false"])
    validate.add_argument(
        "--max-staleness-hours",
        type=int,
        default=None,
        help="Fail if the file is older than this many hours",
    )
    validate.add_argument(
        "--previous-file",
        default=None,
        help="Path to a previous run file to check for large rate movements",
    )

    apply = subparsers.add_parser("apply", help="Resolve and apply competitor rates to config")
    apply.add_argument("--config", required=True)
    apply.add_argument("--source-file")
    apply.add_argument("--write", action="store_true")
    apply.add_argument("--output-dir", default="configs/generated")

    report = subparsers.add_parser("report", help="Print summary of a stored competitor-rate file")
    report.add_argument("--source-file", default=str(DEFAULT_OUTPUT_DIR / "latest.json"))

    return parser


def _apply_bank_categorisation(
    records: list[CompetitorRateRecord],
) -> list[CompetitorRateRecord]:
    """Route each home-loan record through its bank's collector categorisation.

    ``extract_all_sources()`` calls only the generic ``parse_html_rates()`` --
    it has no per-bank knowledge of what an unmarked row means, so every
    home-loan row lands generically "fixed"/"home_loan" with no special,
    standard, or non_comparable tiering and no eligibility implication.
    ``BankPricingCollector.enrich_mortgage_record()`` is the same
    post-processing step ``collect_mortgage_rates()`` already applies; running
    every CLI-fetched record through it here closes that gap so a subsidised
    or closed product can't reach ``market_average``/``market_min``/
    ``market_median`` via the ``competitor_rates apply`` path. A collector is
    reused per bank -- constructing one has no I/O, it only builds the
    (unused) default fetcher closure.
    """
    collectors: dict[str, BankPricingCollector] = {}
    enriched = []
    for record in records:
        if (
            ProductType(record.product_type) != ProductType.HOME_LOAN
            or record.bank not in COLLECTORS_BY_BANK
        ):
            enriched.append(record)
            continue
        collector = collectors.setdefault(record.bank, COLLECTORS_BY_BANK[record.bank]())
        enriched.append(collector.enrich_mortgage_record(record))
    return enriched


def _fetch(args: argparse.Namespace) -> int:
    strict = _parse_bool(args.strict)
    captured_at = datetime.now(timezone.utc)
    records, attempts = extract_all_sources(
        OFFICIAL_SOURCES,
        raw_root=args.raw_dir,
        captured_at_utc=captured_at,
        use_browser_fallback=args.browser_fallback,
    )
    records = _apply_bank_categorisation(records)
    run = _build_run(records=records, attempts=attempts, captured_at_utc=captured_at)
    summary = validate_rate_run(run, strict=strict)
    run = run.model_copy(update={"validation_summary": summary})
    write_rate_run(run, args.output_dir)
    _print_validation_summary(summary)
    for attempt in attempts:
        status = "ok" if attempt.success else "failed"
        print(
            f"{attempt.bank} {attempt.product_type}: {status}, "
            f"records={attempt.records_extracted}, source={attempt.source_url}"
        )
        if attempt.error:
            print(f"  {attempt.error}")
    return 1 if summary.status == "failed" else 0


def _fetch_interest_co_nz(args: argparse.Namespace) -> int:
    strict = _parse_bool(args.strict)
    captured_at = datetime.now(timezone.utc)
    records, attempts = fetch_interest_co_nz_rates(
        raw_root=args.raw_dir,
        captured_at_utc=captured_at,
    )
    run = _build_run(records=records, attempts=attempts, captured_at_utc=captured_at)
    summary = validate_rate_run(run, strict=strict)
    run = run.model_copy(update={"validation_summary": summary})
    write_rate_run(run, args.output_dir)
    _print_validation_summary(summary)
    for attempt in attempts:
        status = "ok" if attempt.success else "failed"
        print(
            f"{attempt.bank} {attempt.product_type}: {status}, "
            f"records={attempt.records_extracted}, source={attempt.source_url}"
        )
        if attempt.error:
            print(f"  {attempt.error}")
    return 1 if summary.status == "failed" else 0


def _validate(args: argparse.Namespace) -> int:
    strict = _parse_bool(args.strict)
    run = load_rate_run(args.source_file)
    previous_run = load_rate_run(args.previous_file) if args.previous_file else None
    summary = validate_rate_run(run, previous_run=previous_run, strict=strict)
    _print_validation_summary(summary)
    failed = summary.status == "failed"
    if args.max_staleness_hours is not None:
        staleness_issues = validate_staleness(run, args.max_staleness_hours, strict=strict)
        for issue in staleness_issues:
            print(f"{issue.severity.upper()}: {issue.message}")
        if any(i.severity == "error" for i in staleness_issues):
            failed = True
    return 1 if failed else 0


def _apply(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    competitor_config = _configured_competitor_rates(config.competitor_rates, args.source_file)
    if not competitor_config.enabled:
        print("Competitor rates are disabled; manual market config remains in use.")
        return 0
    resolved = load_and_resolve_competitor_rates(competitor_config)
    applied = apply_competitor_rates_to_config(
        config.model_copy(update={"competitor_rates": competitor_config})
    )
    print("Competitor assumptions to apply")
    print(
        f"Mortgage competitor reference: {resolved.mortgage_reference_rate:.2%} "
        f"({resolved.mortgage_strategy})"
    )
    print(
        f"Term deposit competitor reference: {resolved.term_deposit_reference_rate:.2%} "
        f"({resolved.term_deposit_strategy})"
    )
    print("Banks included: " + ", ".join(competitor_config.selected_banks))
    print(f"Captured at UTC: {resolved.captured_at_utc.isoformat()}")
    if resolved.validation_warnings:
        print("Validation warnings:")
        for warning in resolved.validation_warnings:
            print(f"  WARNING: {warning}")
    if args.write:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"with_competitor_rates_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.yaml"
        output_path = output_dir / filename
        output_path.write_text(
            yaml.safe_dump(applied.model_dump(mode="json", exclude_none=True), sort_keys=False),
            encoding="utf-8",
        )
        print(f"Generated config: {output_path}")
    return 0


def _report(args: argparse.Namespace) -> int:
    run = load_rate_run(args.source_file)
    print(f"Run ID: {run.run_id}")
    print(f"Captured at UTC: {run.captured_at_utc.isoformat()}")
    print(f"Validation status: {run.validation_summary.status}")
    print("Counts by bank/product:")
    for key in sorted(run.counts_by_bank_product_type):
        print(f"  {key}: {run.counts_by_bank_product_type[key]}")
    if run.validation_summary.issues:
        print("Validation issues:")
        for issue in run.validation_summary.issues:
            print(f"  {issue.severity.upper()}: {issue.message}")
    return 0


def _configured_competitor_rates(
    config: CompetitorRatesConfig | None,
    source_file: str | None,
) -> CompetitorRatesConfig:
    competitor_config = config or CompetitorRatesConfig(enabled=False)
    if source_file:
        competitor_config = competitor_config.model_copy(
            update={"enabled": True, "source_file": source_file}
        )
    return competitor_config


def _build_run(records, attempts, captured_at_utc: datetime) -> CompetitorRateRun:
    counts: dict[str, int] = {}
    for record in records:
        key = f"{record.bank}|{record.product_type}"
        counts[key] = counts.get(key, 0) + 1
    return CompetitorRateRun(
        run_id=str(uuid.uuid4()),
        captured_at_utc=captured_at_utc,
        source_urls_attempted=[attempt.source_url for attempt in attempts],
        source_attempts=attempts,
        validation_summary=ValidationSummary(
            status="failed",
            total_records=len(records),
            valid_records=0,
            warning_count=0,
            error_count=0,
            issues=[],
        ),
        counts_by_bank_product_type=counts,
        records=records,
    )


def _print_validation_summary(summary: ValidationSummary) -> None:
    print(f"Validation status: {summary.status}")
    print(f"Total records: {summary.total_records}")
    print(f"Warnings: {summary.warning_count}")
    print(f"Errors: {summary.error_count}")
    for issue in summary.issues:
        print(f"{issue.severity.upper()}: {issue.message}")


def _parse_bool(value: str) -> bool:
    return value.lower() == "true"


if __name__ == "__main__":
    raise SystemExit(main())

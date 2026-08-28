"""Unattended NZ market-rate collection, for a scheduler to invoke.

Wraps :class:`PricingIntelligenceService` with the things a cron/launchd job
needs and an interactive CLI session does not:

* a single-instance lock, so an overrunning collection cannot interleave with
  the next scheduled one and write two runs into the store at once
* structured JSON logging, reusing ``observability.logging``
* exit codes a scheduler can alert on (see EXIT_* below)
* a machine-readable summary written to disk for the next job in a pipeline

Collection history cannot be backfilled -- a day not collected is gone -- so
this is written to fail loudly rather than exit quietly on a partial run.

    python3 scripts/collect_market_rates_workflow.py --render-javascript

See docs/pricing_intelligence.md for scheduling recipes.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
from pathlib import Path
import sys
from typing import Optional

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.config import load_config
from balance_pricing_engine.observability.logging import configure_json_logging, log_event
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.service import (
    CollectionOutcome,
    PricingIntelligenceService,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

# Under the `balance_pricing_engine` hierarchy so configure_json_logging()'s
# handler actually applies; a bare "pricing_intelligence.*" name falls
# outside that hierarchy and silently drops INFO events while WARNING/ERROR
# fall through to logging's unformatted last-resort handler instead of JSON.
logger = logging.getLogger("balance_pricing_engine.pricing_intelligence.scheduled_collection")

#: Every bank collected and stored.
EXIT_OK = 0
#: No bank could be collected -- the run produced nothing.
EXIT_TOTAL_FAILURE = 1
#: Some banks collected, at least one failed. The market view is incomplete, so
#: a scheduler should surface this rather than treat it as success.
EXIT_PARTIAL_FAILURE = 2
#: Another collection is already running; this invocation did nothing.
EXIT_ALREADY_RUNNING = 3


class _SingleInstanceLock:
    """Advisory file lock ensuring one collection runs at a time.

    A slow browser-rendered collection can overrun its schedule. Without this,
    the next invocation would start against the same store and both would write
    a run, producing duplicate observations and spurious changes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "_SingleInstanceLock":
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunningError(f"another collection holds {self.path}") from exc
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


class AlreadyRunningError(RuntimeError):
    """A collection is already in progress."""


def _banks_with_zero_accepted_rates(outcome: CollectionOutcome) -> set[str]:
    accepted_banks = {record.bank for record in outcome.validation.accepted}
    return {bank for bank in outcome.succeeded_banks if bank not in accepted_banks}


def _validation_only_failures(outcome: CollectionOutcome) -> list[dict]:
    """Banks that fetched successfully but contributed zero accepted rows.

    A bank can fetch and parse cleanly (so it never reaches report.failed)
    while every one of its rows still gets rejected by validation -- e.g. a
    bank-scoped parser regression. Without this, such a bank stays listed as
    succeeded everywhere a scheduler or summary consumer looks, even though
    nothing usable was actually stored for it.
    """
    failures = []
    for bank in sorted(_banks_with_zero_accepted_rates(outcome)):
        codes = sorted(
            {
                issue.code
                for issue in outcome.validation.issues
                if issue.bank == bank and issue.severity == "error"
            }
        )
        failures.append(
            {
                "bank": bank,
                "error_type": "validation_rejected",
                "error": (
                    "every row was rejected by validation ("
                    f"{', '.join(codes) if codes else 'no accepted rows'})"
                ),
            }
        )
    return failures


def _low_coverage_failures(outcome: CollectionOutcome) -> list[dict]:
    """Banks that matched too few of their previously known rate lines.

    A page-layout regression can leave one row parseable while silently
    dropping the rest of a bank's table: the fetch succeeds, at least one row
    is accepted, and nothing is rejected by validation either (the missing
    rows were never parsed at all, so there is nothing to reject) -- so
    neither report.failed nor validation.rejected catches it. The service
    already computes this via its withdrawal-suppression coverage check
    (CollectionOutcome.low_coverage_banks); this surfaces the same signal
    here. Banks with zero accepted rows are reported by
    _validation_only_failures() instead, so they are excluded to avoid a
    duplicate entry.
    """
    zero_accepted = _banks_with_zero_accepted_rates(outcome)
    return [
        {
            "bank": bank,
            "error_type": "low_coverage",
            "error": (
                "matched fewer of this bank's known rate lines than the "
                "configured coverage threshold"
            ),
        }
        for bank in outcome.low_coverage_banks
        if bank not in zero_accepted
    ]


def _partial_rejection_failures(outcome: CollectionOutcome, already_flagged: set[str]) -> list[dict]:
    """Banks with at least one rejected row, not already flagged more specifically.

    exit_code_for() already treats any rejected observation as a partial
    failure (a term-specific parser regression can corrupt a single row
    while the bank's other rows still collect cleanly), but a bank in that
    state has some accepted rows and adequate coverage, so neither
    _validation_only_failures() nor _low_coverage_failures() catches it --
    the summary would report rates_rejected > 0 while listing the affected
    bank as succeeded with no bank-specific entry to point at.
    """
    rejected_banks = {record.bank for record in outcome.validation.rejected}
    failures = []
    for bank in sorted(rejected_banks - already_flagged):
        codes = sorted(
            {
                issue.code
                for issue in outcome.validation.issues
                if issue.bank == bank and issue.severity == "error"
            }
        )
        rejected_count = sum(1 for record in outcome.validation.rejected if record.bank == bank)
        failures.append(
            {
                "bank": bank,
                "error_type": "validation_rejected",
                "error": (
                    f"{rejected_count} row(s) rejected by validation ("
                    f"{', '.join(codes) if codes else 'unknown reason'})"
                ),
            }
        )
    return failures


def _bank_scoped_failures(outcome: CollectionOutcome) -> list[dict]:
    """Every bank-scoped failure not already visible in report.failed.

    Checked in order of specificity so a bank is reported once, under its
    most informative category: zero accepted rows, then coverage below
    threshold, then any remaining bank with at least one rejected row.
    """
    zero_accepted_failures = _validation_only_failures(outcome)
    low_coverage_failures = _low_coverage_failures(outcome)
    already_flagged = {failure["bank"] for failure in zero_accepted_failures} | {
        failure["bank"] for failure in low_coverage_failures
    }
    partial_rejection_failures = _partial_rejection_failures(outcome, already_flagged)
    return zero_accepted_failures + low_coverage_failures + partial_rejection_failures


def build_summary(
    outcome: CollectionOutcome,
    product_type: ProductType | str = ProductType.HOME_LOAN,
) -> dict:
    """Machine-readable summary of one collection run.

    ``product_type`` is embedded so a summary file can be told apart from
    the other product's when both are scheduled -- see ``--product`` and
    ``run_collection()``. Defaults to ``HOME_LOAN`` to match this module's
    other single-product defaults; every existing caller collecting the
    default product keeps working unchanged.
    """
    bank_scoped_failures = _bank_scoped_failures(outcome)
    bank_scoped_failed_banks = {failure["bank"] for failure in bank_scoped_failures}
    return {
        "product_type": ProductType(product_type).value,
        "collected_at_utc": outcome.report.captured_at_utc.isoformat(),
        "banks_succeeded": [
            bank for bank in outcome.succeeded_banks if bank not in bank_scoped_failed_banks
        ],
        "banks_failed": list(outcome.failed_banks) + sorted(bank_scoped_failed_banks),
        "rates_accepted": outcome.rate_count,
        "rates_rejected": len(outcome.validation.rejected),
        "validation_errors": len(outcome.validation.errors),
        "validation_warnings": len(outcome.validation.warnings),
        "new_states_stored": (
            outcome.write_result.inserted if outcome.write_result else 0
        ),
        "unchanged_states": (
            outcome.write_result.unchanged if outcome.write_result else 0
        ),
        "changes_detected": len(outcome.changes),
        "suspicious_changes": sum(1 for change in outcome.changes if change.suspicious),
        "changes": [change.describe() for change in outcome.changes],
        "failures": [
            {"bank": result.bank, "error_type": result.error_type, "error": result.error}
            for result in outcome.report.failed
        ]
        + bank_scoped_failures,
    }


def exit_code_for(outcome: CollectionOutcome) -> int:
    # A run where every bank failed to fetch is a failure, and so is one where
    # pages parsed but validation rejected every record -- either way nothing
    # usable was stored, and a scheduler must not treat that as success.
    if not outcome.report.any_success or outcome.rate_count == 0:
        return EXIT_TOTAL_FAILURE
    # Any rejected observation -- not just a bank where every row was
    # rejected -- means the collection is incomplete: a term-specific parser
    # regression can silently drop one product from an otherwise-healthy
    # bank. A low-coverage bank catches the case validation can't: rows that
    # were never parsed at all (not rejected -- simply missing) because a
    # page-layout regression silently dropped most of a bank's table.
    # Warnings (e.g. a suspicious but plausible movement) stay non-fatal.
    if outcome.report.failed or outcome.validation.rejected or outcome.low_coverage_banks:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def run_collection(
    config: PricingIntelligenceConfig,
    render_javascript: bool,
    summary_path: Optional[Path] = None,
    product_type: ProductType | str = ProductType.HOME_LOAN,
) -> tuple[int, dict]:
    """Run one collection and return ``(exit_code, summary)``.

    ``product_type`` defaults to ``HOME_LOAN``, matching ``service.collect()``'s
    own default, so an existing scheduled deployment collects exactly what it
    always has. Collecting term deposits too needs a second scheduled
    invocation with ``--product term_deposit`` (and its own ``--lock-file``/
    ``--summary-file``, so it does not collide with the home-loan run) -- see
    ``docs/pricing_intelligence.md``.
    """
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        render_javascript=render_javascript,
    ) as service:
        outcome = service.collect(product_type=product_type)

    summary = build_summary(outcome, product_type)
    code = exit_code_for(outcome)

    if code == EXIT_TOTAL_FAILURE:
        completion_level = logging.ERROR
    elif code == EXIT_PARTIAL_FAILURE:
        # In the launchd deployment this is the only structured health event,
        # so alerting that filters on warning/error severity must still see
        # an incomplete market view -- INFO would let it slip through
        # unnoticed even though exit_code_for() already treats it as a
        # failure.
        completion_level = logging.WARNING
    else:
        completion_level = logging.INFO

    log_event(
        logger,
        "market collection finished",
        level=completion_level,
        exit_code=code,
        **{
            key: summary[key]
            for key in (
                "banks_succeeded",
                "banks_failed",
                "rates_accepted",
                "new_states_stored",
                "changes_detected",
                "suspicious_changes",
            )
        },
    )
    for change in outcome.changes:
        log_event(
            logger,
            "market change detected",
            level=logging.WARNING if change.suspicious else logging.INFO,
            bank=change.bank,
            change_type=change.change_type.value,
            change_bps=change.change_bps,
            suspicious=change.suspicious,
        )

    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return code, summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to an EngineConfig YAML (e.g. configs/base.yaml) to load "
            "pricing_intelligence settings (selected banks, validation "
            "thresholds, collection delays, database path) from; --database "
            "still overrides just the database path"
        ),
    )
    parser.add_argument("--database", default=None, help="Observation store path")
    parser.add_argument(
        "--product",
        default=ProductType.HOME_LOAN.value,
        choices=[ProductType.HOME_LOAN.value, ProductType.TERM_DEPOSIT.value],
        help=(
            "Which market to collect (default: home_loan). Term deposits need "
            "a separate scheduled invocation with --product term_deposit and "
            "its own --lock-file/--summary-file, so it does not collide with "
            "the home-loan schedule -- see docs/pricing_intelligence.md."
        ),
    )
    parser.add_argument(
        "--render-javascript",
        action="store_true",
        help="Render pages in a headless browser. Required for live collection.",
    )
    parser.add_argument(
        "--lock-file",
        default="data/processed/pricing_intelligence/collection.lock",
        help="Single-instance lock path",
    )
    parser.add_argument(
        "--summary-file", default=None, help="Write a JSON run summary here"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the text summary")
    args = parser.parse_args(argv)

    configure_json_logging()

    config = PricingIntelligenceConfig()
    if args.config:
        # configs/base.yaml's pricing_intelligence block would otherwise be
        # silently ignored -- scheduled production runs could then contact
        # excluded banks and validate/store observations under different
        # policy from interactive runs, per the same pattern already fixed
        # in the pricing_intelligence CLI.
        engine_config = load_config(args.config)
        if engine_config.pricing_intelligence is not None:
            config = engine_config.pricing_intelligence
    if args.database:
        config = config.model_copy(update={"database_path": args.database})

    try:
        with _SingleInstanceLock(Path(args.lock_file)):
            code, summary = run_collection(
                config,
                render_javascript=args.render_javascript,
                summary_path=Path(args.summary_file) if args.summary_file else None,
                product_type=args.product,
            )
    except AlreadyRunningError as exc:
        log_event(logger, "collection skipped", level=logging.WARNING, reason=str(exc))
        return EXIT_ALREADY_RUNNING

    if not args.quiet:
        print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())

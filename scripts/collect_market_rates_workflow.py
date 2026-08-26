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

from balance_pricing_engine.observability.logging import configure_json_logging, log_event
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.service import (
    CollectionOutcome,
    PricingIntelligenceService,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

logger = logging.getLogger("pricing_intelligence.scheduled_collection")

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


def build_summary(outcome: CollectionOutcome) -> dict:
    """Machine-readable summary of one collection run."""
    return {
        "collected_at_utc": outcome.report.captured_at_utc.isoformat(),
        "banks_succeeded": list(outcome.succeeded_banks),
        "banks_failed": list(outcome.failed_banks),
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
        ],
    }


def exit_code_for(outcome: CollectionOutcome) -> int:
    # A run where every bank failed to fetch is a failure, and so is one where
    # pages parsed but validation rejected every record -- either way nothing
    # usable was stored, and a scheduler must not treat that as success.
    if not outcome.report.any_success or outcome.rate_count == 0:
        return EXIT_TOTAL_FAILURE
    # A bank can fetch and parse cleanly (so it never reaches report.failed)
    # while every one of its rows still gets rejected by validation -- a
    # bank-scoped parser regression. That bank contributed nothing, so it
    # must be judged the same as an outright fetch failure, not silently
    # folded into the aggregate rate_count from the other banks.
    accepted_banks = {record.bank for record in outcome.validation.accepted}
    banks_without_accepted_rates = [
        bank for bank in outcome.succeeded_banks if bank not in accepted_banks
    ]
    if outcome.report.failed or banks_without_accepted_rates:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def run_collection(
    config: PricingIntelligenceConfig,
    render_javascript: bool,
    summary_path: Optional[Path] = None,
) -> tuple[int, dict]:
    """Run one collection and return ``(exit_code, summary)``."""
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        render_javascript=render_javascript,
    ) as service:
        outcome = service.collect()

    summary = build_summary(outcome)
    code = exit_code_for(outcome)

    log_event(
        logger,
        "market collection finished",
        level=logging.ERROR if code == EXIT_TOTAL_FAILURE else logging.INFO,
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
    parser.add_argument("--database", default=None, help="Observation store path")
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
    if args.database:
        config = config.model_copy(update={"database_path": args.database})

    try:
        with _SingleInstanceLock(Path(args.lock_file)):
            code, summary = run_collection(
                config,
                render_javascript=args.render_javascript,
                summary_path=Path(args.summary_file) if args.summary_file else None,
            )
    except AlreadyRunningError as exc:
        log_event(logger, "collection skipped", level=logging.WARNING, reason=str(exc))
        return EXIT_ALREADY_RUNNING

    if not args.quiet:
        print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())

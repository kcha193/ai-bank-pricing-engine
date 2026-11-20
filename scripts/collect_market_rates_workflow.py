"""Unattended NZ market-rate collection, for a scheduler to invoke.

Wraps :class:`PricingIntelligenceService` with the things a cron/launchd job
needs and an interactive CLI session does not:

* a single-instance lock, so an overrunning collection cannot interleave with
  the next scheduled one and write two runs into the store at once
* structured JSON logging, reusing ``observability.logging``
* exit codes a scheduler can alert on (see EXIT_* below)
* a machine-readable summary written to disk for the next job in a pipeline
* alert delivery: detected rate moves are pushed to a webhook and/or Telegram
  when configured (see ``pricing_intelligence.alerts``). Unset by default,
  and a broken channel never fails the run.
* collector health tracking and escalation, and a persisted
  ``RunRecord`` -- both via ``pricing_intelligence.orchestration.
  run_operational_cycle``, the same function the interactive
  ``pricing_intelligence run`` CLI command uses, so scheduled and manual
  runs classify and alert identically.

* an end-to-end deadline (DATA-008): by default the collection runs in a
  supervised worker process that is terminated -- with its browser processes
  -- at ``collection_run_timeout_seconds`` (or ``--run-timeout-seconds``); the
  supervisor then writes a classified ``collection_timeout`` summary and,
  when the database allows, a FAILED run record. ``--no-supervisor`` runs
  in-process with only the cooperative deadline (tests, debugging).
* classified failure summaries for a locked or failing database (DATA-009)
  and for any other error that escapes the cycle, always written atomically
* escalation of repeated "already running" skips (exit 3), tracked per
  product in a sidecar next to the lock file (see ``skip_escalation``)

Collection history cannot be backfilled -- a day not collected is gone -- so
this is written to fail loudly rather than exit quietly on a partial run.

    python3 scripts/collect_market_rates_workflow.py --render-javascript

See docs/pricing_intelligence.md for scheduling recipes.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import signal
import sqlite3
import sys
from typing import Optional
import uuid

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.competitor_rates.run_deadline import (
    COLLECTION_TIMEOUT,
    CollectionTimeoutError,
    RunDeadline,
)
from balance_pricing_engine.config import load_config
from balance_pricing_engine.observability.logging import configure_json_logging, log_event
from balance_pricing_engine.pricing_intelligence.config import (
    PricingIntelligenceConfig,
    resolve_database_path_from_env,
)
from balance_pricing_engine.pricing_intelligence.locking import (
    AlreadyRunningError,
    SingleInstanceLock as _SingleInstanceLock,
)
# Several of these (EXIT_OK, the three underscored helpers, exit_code_for)
# are not called directly in this module any more -- they are re-imported
# purely so this module's own namespace still carries them, for any test or
# tool written against the pre-relocation module (see orchestration.py's
# docstring for why they moved).
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,  # noqa: F401
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    _banks_with_zero_comparable_rates,  # noqa: F401
    _comparable_rate_count,  # noqa: F401
    _validation_only_failures,  # noqa: F401
    build_failure_summary,
    build_replay_summary,
    build_summary,
    default_lock_file_path,
    exit_code_for,  # noqa: F401
    record_failed_attempt,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.alerts import (
    build_telegram_sink_from_env,
    build_webhook_sink_from_env,
)
from balance_pricing_engine.pricing_intelligence.run_artifacts import read_json, write_json_atomically
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.skip_escalation import (
    record_lock_acquired,
    record_skip,
    skip_state_path,
)
from balance_pricing_engine.pricing_intelligence.sqlite_errors import (
    DATABASE_LOCKED,
    classify_sqlite_error,
)
from balance_pricing_engine.pricing_intelligence.store import (
    ChronologicalOrderError,
    ObservationStore,
)
from balance_pricing_engine.pricing_intelligence.supervisor import (
    DEFAULT_GRACE_SECONDS,
    run_supervised,
)

# Under the `balance_pricing_engine` hierarchy so configure_json_logging()'s
# handler actually applies; a bare "pricing_intelligence.*" name falls
# outside that hierarchy and silently drops INFO events while WARNING/ERROR
# fall through to logging's unformatted last-resort handler instead of JSON.
logger = logging.getLogger("balance_pricing_engine.pricing_intelligence.scheduled_collection")

#: Busy timeout the supervisor uses when recording a timed-out run: short, because the
#: run it is reporting on may be the reason the database is unavailable.
SUPERVISOR_RECORD_BUSY_TIMEOUT_SECONDS = 10.0


def _write_summary(summary_path: Optional[Path], summary: dict) -> None:
    if summary_path is not None:
        write_json_atomically(summary_path, summary)


def _clock_regression_failure(
    error: ChronologicalOrderError,
    product_type: ProductType,
    summary_path: Optional[Path],
    run_id: Optional[str] = None,
) -> tuple[int, dict]:
    """Report a backwards clock as a total failure, with a written summary.

    Carries every key :func:`build_summary` produces so a consumer reading a
    summary file (the collector canary, launchd alerting) does not have to
    special-case this shape -- an empty run, one synthetic failure entry, and
    the guard's own message as the reason. No bank was reached, so
    ``banks_failed`` is ``["*"]``: alerting that keys off it still needs
    something to fire on, and an empty list would read as "nothing went wrong".
    """
    summary = build_failure_summary(
        product_type,
        error_type=type(error).__name__,
        error=str(error),
        failure_type="clock_regression",
        run_id=run_id,
    )
    log_event(
        logger,
        "market collection aborted",
        level=logging.ERROR,
        exit_code=EXIT_TOTAL_FAILURE,
        reason=str(error),
        product_type=product_type.value,
    )
    _write_summary(summary_path, summary)
    return EXIT_TOTAL_FAILURE, summary


def _record_failure_safely(
    store: ObservationStore,
    *,
    run_id: str,
    product_type: ProductType,
    started_at: datetime,
    banks: tuple[str, ...],
    error_summary: str,
) -> tuple[bool, str]:
    """Best-effort FAILED run record; a database problem is reported, never claimed."""
    try:
        return record_failed_attempt(
            store,
            run_id=run_id,
            product_type=product_type,
            started_at=started_at,
            banks_attempted=banks,
            error_summary=error_summary,
        )
    except sqlite3.Error as exc:
        error_type = classify_sqlite_error(exc)
        logger.error("could not record failed run %s: %s (%s)", run_id, exc, error_type)
        return False, f"run record not written: {error_type}: {exc}"


def _timeout_failure(
    error: CollectionTimeoutError,
    *,
    store: Optional[ObservationStore],
    product_type: ProductType,
    summary_path: Optional[Path],
    run_id: str,
    started_at: datetime,
    banks: tuple[str, ...],
) -> tuple[int, dict]:
    persisted, note = (
        _record_failure_safely(
            store,
            run_id=run_id,
            product_type=product_type,
            started_at=started_at,
            banks=banks,
            error_summary=f"{COLLECTION_TIMEOUT}: {error}",
        )
        if store is not None
        else (False, "run record not attempted")
    )
    summary = build_failure_summary(
        product_type,
        error_type=COLLECTION_TIMEOUT,
        error=str(error),
        failure_type=COLLECTION_TIMEOUT,
        run_id=run_id,
        run_record_persisted=persisted,
        run_record_note=note,
        started_at_utc=started_at.isoformat(),
        deadline_at_utc=(started_at + timedelta(seconds=error.timeout_seconds)).isoformat(),
        timeout_seconds=error.timeout_seconds,
        elapsed_seconds=round(error.elapsed_seconds, 3),
        stage=error.stage,
    )
    log_event(
        logger,
        "market collection timed out",
        level=logging.ERROR,
        exit_code=EXIT_TOTAL_FAILURE,
        failure_type=COLLECTION_TIMEOUT,
        product_type=product_type.value,
        run_id=run_id,
        stage=error.stage,
        timeout_seconds=error.timeout_seconds,
        run_record_persisted=persisted,
    )
    _write_summary(summary_path, summary)
    return EXIT_TOTAL_FAILURE, summary


def _database_failure(
    error: sqlite3.Error,
    *,
    product_type: ProductType,
    summary_path: Optional[Path],
    run_id: str,
    database_path: str,
) -> tuple[int, dict]:
    """Classify a database failure without claiming earlier transactions rolled back.

    Collection and later bookkeeping commit independently. Inspect committed participation
    through a bounded read-only connection; an unreadable database leaves evidence unknown.
    """
    error_type = classify_sqlite_error(error) or type(error).__name__
    failure_type = DATABASE_LOCKED if error_type == DATABASE_LOCKED else "database_error"
    committed = None
    record_present = False
    counts = None
    try:
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as reader:
            reader.execute("BEGIN")
            counts = reader.execute(
                "SELECT inserted_count, unchanged_count FROM collection_run_products "
                "WHERE run_id = ? AND product_type = ?",
                (run_id, product_type.value),
            ).fetchone()
            committed = counts is not None
            record_present = (
                reader.execute(
                    "SELECT 1 FROM operational_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                is not None
            )
    except (sqlite3.Error, OSError):
        logger.warning("Could not establish committed run evidence after database failure")
    note = (
        "database failure; earlier observation commits remain recorded"
        if committed
        else "database failure; committed observation evidence is "
        + ("absent" if committed is False else "unknown")
    )
    summary = build_failure_summary(
        product_type,
        error_type=error_type,
        error=str(error),
        failure_type=failure_type,
        run_id=run_id,
        run_record_persisted=record_present,
        run_record_note=note,
        observations_committed=committed,
        rates_accepted=sum(counts) if counts else (0 if committed is False else None),
        new_states_stored=counts[0] if counts else (0 if committed is False else None),
        unchanged_states=counts[1] if counts else (0 if committed is False else None),
    )
    log_event(
        logger,
        "market collection failed on the observation store",
        level=logging.ERROR,
        exit_code=EXIT_TOTAL_FAILURE,
        failure_type=failure_type,
        error_type=error_type,
        product_type=product_type.value,
        run_id=run_id,
        database=Path(database_path).name,
    )
    _write_summary(summary_path, summary)
    return EXIT_TOTAL_FAILURE, summary


def run_collection(
    config: PricingIntelligenceConfig,
    render_javascript: bool,
    summary_path: Optional[Path] = None,
    product_type: ProductType | str = ProductType.HOME_LOAN,
    webhook_text_field: str = "text",
    pricing_config: Optional[object] = None,
    run_id: Optional[str] = None,
    deadline: Optional[RunDeadline] = None,
) -> tuple[int, dict]:
    """Run one collection and return ``(exit_code, summary)``.

    Never raises for an operational failure: a timeout (``deadline``), a locked or
    failing database, a backwards clock or any other unexpected error becomes a total
    failure (exit 1) with a classified summary written to ``summary_path``. ``run_id``
    is the operational run id (minted when omitted) and appears in every summary.

    ``product_type`` defaults to ``HOME_LOAN``, matching ``service.collect()``'s
    own default, so an existing scheduled deployment collects exactly what it
    always has. Collecting term deposits too needs a second scheduled
    invocation with ``--product term_deposit`` (and its own ``--lock-file``/
    ``--summary-file``, so it does not collide with the home-loan run) -- see
    ``docs/pricing_intelligence.md``.

    Detected rate moves are pushed to a webhook and/or Telegram when
    configured (see ``pricing_intelligence.alerts``); ``webhook_text_field``
    ("text" for Slack/Teams, "content" for Discord) only affects the webhook
    payload's message field.

    ``pricing_config`` (the full ``EngineConfig`` loaded from ``--config``,
    ``None`` when this ran on ``--database`` alone) enables auto-triggered
    pricing scenarios: every tracked/comparable term with a genuine rate move
    this run gets a scenario comparison built via
    ``pricing_scenario_trigger.run_triggered_scenarios`` -- imported here,
    not inside ``pricing_intelligence``, which is architecturally forbidden
    from depending on the optimiser (see
    ``tests/unit/test_pricing_intelligence_boundaries.py``). ``None`` (the
    default, matching a ``--database``-only invocation with no economic
    assumptions loaded) simply skips scenario-triggering.
    """
    product_type = ProductType(product_type)
    scenario_trigger_fn = None
    if pricing_config is not None:
        from balance_pricing_engine.pricing_intelligence.orchestration import ScenarioResult
        from balance_pricing_engine.pricing_scenario_trigger import (
            format_scenario_block,
            run_triggered_scenarios,
        )

        def scenario_trigger_fn(terms, snapshot):
            # Isolated per term: orchestration.run_operational_cycle() already
            # catches a failure from this whole callback so one can't abort
            # collection (see its own docstring), but that all-or-nothing
            # catch still discards every other term's already-computed
            # comparison if only one term raises -- e.g. a single term has no
            # external competitor peer left once our_bank is excluded, a
            # documented reachable case, not a bug in the others. A per-term
            # try/except here means the unaffected terms are still persisted
            # and attached to their own alerts.
            results = {}
            for term in terms:
                try:
                    comparison = run_triggered_scenarios(
                        pricing_config,
                        term_months=term,
                        snapshot=snapshot,
                        # The collecting store's own coverage policy (DATA-003): an
                        # under-covered term raises and is skipped below, never priced.
                        min_banks=config.min_banks,
                    )
                except Exception:  # noqa: BLE001 - decision-support only, one term must not cost the rest
                    logger.warning(
                        "scenario comparison failed for term=%s; continuing with the "
                        "remaining triggered terms",
                        term,
                        exc_info=True,
                    )
                    continue
                results[term] = ScenarioResult(
                    data=comparison.as_dicts(), alert_block=format_scenario_block(comparison)
                )
            return results

    run_id = run_id or uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)
    try:
        with PricingIntelligenceService(
            config=config,
            store=ObservationStore(
                config.database_path, busy_timeout_seconds=config.sqlite_busy_timeout_seconds
            ),
            render_javascript=render_javascript,
        ) as service:
            try:
                result = run_operational_cycle(
                    service,
                    product_type=product_type,
                    webhook_text_field=webhook_text_field,
                    scenario_trigger_fn=scenario_trigger_fn,
                    run_id=run_id,
                    deadline=deadline,
                )
            except ChronologicalOrderError as exc:
                # service.collect() stamps its own datetime.now, so this fires on
                # an ordinary scheduled run whenever the wall clock steps
                # backwards behind the last stored run. Caught here specifically
                # (not as a bare ValueError, which would swallow real bugs) so the
                # worker still writes a summary for the canary to upload and
                # returns a classified code instead of an unhandled traceback.
                return _clock_regression_failure(exc, product_type, summary_path, run_id)
            except CollectionTimeoutError as exc:
                return _timeout_failure(
                    exc,
                    store=service.store,
                    product_type=product_type,
                    summary_path=summary_path,
                    run_id=run_id,
                    started_at=started_at,
                    banks=tuple(config.selected_banks),
                )
            outcome = result.outcome
    except sqlite3.Error as exc:
        return _database_failure(
            exc,
            product_type=product_type,
            summary_path=summary_path,
            run_id=run_id,
            database_path=config.database_path,
        )
    except Exception as exc:  # noqa: BLE001 - classified, logged with traceback, summarised
        logger.exception("market collection failed unexpectedly (run %s)", run_id)
        summary = build_failure_summary(
            product_type,
            error_type=type(exc).__name__,
            error=str(exc),
            failure_type="unexpected_error",
            run_id=run_id,
            run_record_persisted=False,
            run_record_note="the run stopped before its run record was written",
        )
        _write_summary(summary_path, summary)
        return EXIT_TOTAL_FAILURE, summary

    summary = (
        build_replay_summary(outcome, result.run_record, product_type)
        if outcome.already_recorded
        else build_summary(outcome, product_type, quarantine=result.quarantine)
    )
    summary["alerts_sent"] = result.alerts_sent
    code = result.code
    # Additive fields: the run id ties this file to the operational ledger, and
    # run_record_persisted states whether that ledger row exists.
    summary["run_id"] = run_id
    summary["status"] = result.status
    summary["failure_type"] = None
    summary["run_record_persisted"] = result.run_record is not None
    if outcome.already_recorded and result.run_record is None:
        code = EXIT_TOTAL_FAILURE
        summary["status"] = "FAILED"
        summary["failure_type"] = "incomplete_run_record"

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
                "alerts_sent",
            )
        },
    )
    for change in (() if outcome.already_recorded else outcome.changes):
        log_event(
            logger,
            "market change detected",
            level=logging.WARNING if change.suspicious else logging.INFO,
            bank=change.bank,
            change_type=change.change_type.value,
            change_bps=change.change_bps,
            suspicious=change.suspicious,
        )

    _write_summary(summary_path, summary)

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
        default=None,
        help=(
            "Single-instance lock path (default: derived from --product, "
            "matching the checked-in cron/launchd artifacts' per-product "
            "lock files -- see deploy/scheduling/ and "
            "docs/pricing_intelligence.md)"
        ),
    )
    parser.add_argument(
        "--summary-file", default=None, help="Write a JSON run summary here"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the text summary")
    parser.add_argument(
        "--webhook-text-field",
        default="text",
        help=(
            "JSON field the webhook payload's message goes in when "
            "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL is set. The default "
            "('text') is read directly by Slack and Teams webhooks; use "
            "'content' for Discord."
        ),
    )
    parser.add_argument(
        "--run-timeout-seconds",
        type=float,
        default=None,
        help=(
            "End-to-end deadline for this product's collection (default: the config's "
            "collection.collection_run_timeout_seconds, 1800). The worker process and its "
            "browser processes are terminated when it passes; the run exits 1 with a "
            "collection_timeout summary."
        ),
    )
    parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help=(
            "Run the collection in this process with only the cooperative deadline "
            "check, instead of a supervised worker that can be force-terminated. For "
            "tests and debugging; scheduled runs should keep the supervisor."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Operational run id (default: a fresh id). A scheduler retrying the same "
            "invocation may pass the same id: its run stays idempotent and a repeated "
            "skip is not counted twice toward exit-3 escalation."
        ),
    )
    # Internal: set by the supervisor on its worker. The worker inherits the
    # supervisor's lock descriptor, so it must not try to take the lock again.
    parser.add_argument("--worker-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-deadline-seconds", type=float, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    configure_json_logging()

    config, engine_config = _resolve_config(args)
    run_id = args.run_id or uuid.uuid4().hex
    product_type = ProductType(args.product)
    summary_path = Path(args.summary_file) if args.summary_file else None
    hard_timeout = args.run_timeout_seconds or config.collection.collection_run_timeout_seconds

    if args.worker_mode:
        soft = args.worker_deadline_seconds or hard_timeout
        code, summary = run_collection(
            config,
            render_javascript=args.render_javascript,
            summary_path=summary_path,
            product_type=product_type,
            webhook_text_field=args.webhook_text_field,
            pricing_config=engine_config,
            run_id=run_id,
            deadline=RunDeadline(soft),
        )
        if not args.quiet:
            print(json.dumps(summary, indent=2))
        return code

    lock_file = Path(args.lock_file or default_lock_file_path(args.product, config.database_path))
    sinks = _alert_sinks(args.webhook_text_field)
    lock = _SingleInstanceLock(lock_file)
    try:
        lock.__enter__()
    except AlreadyRunningError as exc:
        log_event(logger, "collection skipped", level=logging.WARNING, reason=str(exc))
        _escalate_skip(lock_file, product_type, run_id, config, sinks)
        return EXIT_ALREADY_RUNNING
    try:
        _clear_skip_streak(lock_file, product_type, sinks)
        if args.no_supervisor:
            code, summary = run_collection(
                config,
                render_javascript=args.render_javascript,
                summary_path=summary_path,
                product_type=product_type,
                webhook_text_field=args.webhook_text_field,
                pricing_config=engine_config,
                run_id=run_id,
                deadline=RunDeadline(hard_timeout),
            )
        else:
            code, summary = _supervise(
                argv if argv is not None else sys.argv[1:],
                lock=lock,
                config=config,
                product_type=product_type,
                summary_path=summary_path,
                run_id=run_id,
                hard_timeout=hard_timeout,
            )
    finally:
        lock.__exit__(None, None, None)

    if not args.quiet and summary is not None:
        print(json.dumps(summary, indent=2))
    return code


def _resolve_config(args: argparse.Namespace) -> tuple[PricingIntelligenceConfig, Optional[object]]:
    config = resolve_database_path_from_env(PricingIntelligenceConfig())
    # Retained (not just its .pricing_intelligence block) so scheduled runs
    # can auto-trigger pricing scenarios -- see run_collection()'s
    # pricing_config docstring.
    engine_config = None
    if args.config:
        # configs/base.yaml's pricing_intelligence block would otherwise be
        # silently ignored -- scheduled production runs could then contact
        # excluded banks and validate/store observations under different
        # policy from interactive runs, per the same pattern already fixed
        # in the pricing_intelligence CLI.
        engine_config = load_config(args.config)
        if engine_config.pricing_intelligence is not None:
            yaml_config = engine_config.pricing_intelligence
            if "database_path" in yaml_config.model_fields_set:
                config = yaml_config
            else:
                # The YAML block didn't set database_path itself, so
                # Pydantic filled in the hard-coded default -- adopting
                # yaml_config wholesale here would silently discard the
                # PRICING_INTELLIGENCE_DB_PATH override resolved above.
                config = yaml_config.model_copy(update={"database_path": config.database_path})
    if args.database:
        config = config.model_copy(update={"database_path": args.database})
    return config, engine_config


def _alert_sinks(webhook_text_field: str) -> dict:
    sinks: dict = {}
    webhook = build_webhook_sink_from_env(text_field=webhook_text_field)
    if webhook is not None:
        sinks["webhook"] = webhook
    telegram = build_telegram_sink_from_env()
    if telegram is not None:
        sinks["telegram"] = telegram
    return sinks


def _escalate_skip(lock_file: Path, product_type, run_id, config, sinks) -> None:
    """Count this skip toward the product's exit-3 streak. Never changes the exit code."""
    try:
        record_skip(
            skip_state_path(lock_file),
            product_type=product_type,
            invocation_id=run_id,
            policy=config.skip_escalation,
            sinks=sinks,
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - escalation bookkeeping must not mask exit 3
        logger.exception("could not record skip escalation for %s", lock_file.name)


def _clear_skip_streak(lock_file: Path, product_type, sinks) -> None:
    try:
        record_lock_acquired(
            skip_state_path(lock_file),
            product_type=product_type,
            sinks=sinks,
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - recovery bookkeeping must not block the collection
        logger.exception("could not clear skip escalation for %s", lock_file.name)


def _soft_deadline_seconds(hard_timeout: float) -> float:
    """The worker's own cooperative deadline: early enough that a run which is merely
    slow stops cleanly and writes its own classified summary before the supervisor's
    hard kill (10% of the budget, at most 60 s, earlier)."""
    return max(hard_timeout - min(60.0, hard_timeout * 0.1), hard_timeout * 0.5)


def _worker_command(argv: list[str], *, run_id: str, soft_deadline_seconds: float) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        *argv,
        "--worker-mode",
        "--run-id",
        run_id,
        "--worker-deadline-seconds",
        repr(soft_deadline_seconds),
    ]


def _supervise(
    argv: list[str],
    *,
    lock,
    config: PricingIntelligenceConfig,
    product_type: ProductType,
    summary_path: Optional[Path],
    run_id: str,
    hard_timeout: float,
) -> tuple[int, Optional[dict]]:
    """Run the collection in a worker process under a hard deadline (DATA-008).

    The worker inherits the lock descriptor, so the lock stays held while either
    process lives. On timeout the worker's whole process tree is terminated, then this
    supervisor writes the timeout summary and, if the database allows, a FAILED run
    record. A worker that exits abnormally, or without its own summary, is reported as
    ``worker_failed``. Otherwise the worker's own exit code and summary stand.
    """
    soft = _soft_deadline_seconds(hard_timeout)
    started_at = datetime.now(timezone.utc)
    previous_handler = None
    try:
        # A scheduler stopping this supervisor (launchctl unload, kill) must take the
        # worker down with it, not orphan it: SIGTERM becomes SystemExit, which
        # run_supervised() answers by terminating the worker tree.
        previous_handler = signal.signal(
            signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum)
        )
    except ValueError:  # not the main thread (e.g. an embedding caller)
        previous_handler = None
    try:
        outcome = run_supervised(
            _worker_command(argv, run_id=run_id, soft_deadline_seconds=soft),
            timeout_seconds=hard_timeout,
            grace_seconds=DEFAULT_GRACE_SECONDS,
            pass_fds=(lock.fileno(),),
        )
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)

    supervisor = {
        "worker_pid": outcome.pid,
        "worker_exit_code": outcome.returncode,
        "worker_terminated": outcome.timed_out,
        "terminated_process_count": len(outcome.terminated_pids),
        "killed_after_grace": outcome.killed_after_grace,
        "hard_timeout_seconds": hard_timeout,
        "worker_soft_deadline_seconds": soft,
    }
    if not outcome.timed_out:
        worker_summary = None
        if summary_path is not None and summary_path.exists():
            try:
                worker_summary = read_json(summary_path)
            except (OSError, ValueError):
                worker_summary = None
        own_summary = isinstance(worker_summary, dict) and worker_summary.get("run_id") == run_id
        if outcome.returncode in (EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE) and (
            summary_path is None or own_summary
        ):
            return outcome.returncode, None

    failure_type = COLLECTION_TIMEOUT if outcome.timed_out else "worker_failed"
    error = (
        f"collection worker exceeded the {hard_timeout:g}s run deadline and was terminated"
        if outcome.timed_out
        else f"collection worker exited with code {outcome.returncode} without its own summary"
    )
    persisted, note = False, "run record not attempted"
    try:
        with ObservationStore(
            config.database_path,
            busy_timeout_seconds=min(
                SUPERVISOR_RECORD_BUSY_TIMEOUT_SECONDS, config.sqlite_busy_timeout_seconds
            ),
        ) as store:
            persisted, note = _record_failure_safely(
                store,
                run_id=run_id,
                product_type=product_type,
                started_at=started_at,
                banks=tuple(config.selected_banks),
                error_summary=f"{failure_type}: {error}",
            )
    except sqlite3.Error as exc:
        note = f"run record not written: {classify_sqlite_error(exc)}: {exc}"
    summary = build_failure_summary(
        product_type,
        error_type=failure_type,
        error=error,
        failure_type=failure_type,
        run_id=run_id,
        run_record_persisted=persisted,
        run_record_note=note,
        started_at_utc=started_at.isoformat(),
        deadline_at_utc=(started_at + timedelta(seconds=hard_timeout)).isoformat(),
        timeout_seconds=hard_timeout,
        elapsed_seconds=round(outcome.elapsed_seconds, 3),
        supervisor=supervisor,
    )
    log_event(
        logger,
        "market collection worker stopped",
        level=logging.ERROR,
        exit_code=EXIT_TOTAL_FAILURE,
        failure_type=failure_type,
        product_type=product_type.value,
        run_id=run_id,
        run_record_persisted=persisted,
        **{key: value for key, value in supervisor.items() if key != "worker_pid"},
    )
    _write_summary(summary_path, summary)
    return EXIT_TOTAL_FAILURE, summary


if __name__ == "__main__":
    sys.exit(main())

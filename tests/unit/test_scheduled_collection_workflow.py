"""Unattended collection workflow: exit codes, locking, and run summaries.

Runs entirely against saved fixtures -- a scheduled job's behaviour must be
testable without reaching a live bank site.
"""

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _minimal_engine_config(**pricing_intelligence_overrides):
    """A minimal EngineConfig YAML body, mirroring test_pricing_intelligence_cli.py."""
    return {
        "random_seed": 1,
        "simulation": {"n_loan_customers": 100, "n_deposit_customers": 100},
        "market": {
            "cash_rate": 0.04,
            "wholesale_funding_spread": 0.01,
            "competitor_loan_rate": 0.06,
            "competitor_deposit_rate": 0.04,
            "home_loan_reference_rate": 0.0525,
            "term_deposit_reference_rate": 0.0400,
        },
        "pricing_strategy": {
            "home_loan_spread_bps": {"min": -50, "max": 50, "step": 5},
            "term_deposit_spread_bps": {"min": -40, "max": 40, "step": 5},
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": 0.65,
            "funding_shortfall_penalty_rate": 0.02,
        },
        "response": {
            "loan_rate_elasticity": -85.0,
            "loan_competitor_gap_elasticity": -65.0,
            "deposit_rate_elasticity": 95.0,
            "deposit_competitor_gap_elasticity": 75.0,
        },
        "pricing_intelligence": pricing_intelligence_overrides,
    }


def load_script():
    path = Path("scripts/collect_market_rates_workflow.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workflow():
    return load_script()


def collect_outcome(fetcher, tmp_path, captured_at=DAY_ONE):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fetcher
    ) as service:
        return service.collect(captured_at_utc=captured_at, persist_snapshot=False)


def test_full_success_exits_zero(workflow, fixture_fetcher, tmp_path):
    outcome = collect_outcome(fixture_fetcher(), tmp_path)

    assert workflow.exit_code_for(outcome) == workflow.EXIT_OK


def test_partial_failure_is_distinguishable_from_success(workflow, fixture_fetcher, tmp_path):
    """An incomplete market view must not be reported as a clean run."""
    outcome = collect_outcome(fixture_fetcher(failing_banks=["Westpac"]), tmp_path)

    assert workflow.exit_code_for(outcome) == workflow.EXIT_PARTIAL_FAILURE
    assert workflow.EXIT_PARTIAL_FAILURE != workflow.EXIT_OK


def test_total_failure_exits_nonzero(workflow, fixture_fetcher, tmp_path):
    banks = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]
    outcome = collect_outcome(fixture_fetcher(failing_banks=banks), tmp_path)

    assert workflow.exit_code_for(outcome) == workflow.EXIT_TOTAL_FAILURE


def test_all_rates_rejected_is_a_total_failure(workflow, fixture_fetcher, tmp_path):
    """A shared parser regression producing implausible rates must not exit clean.

    Every bank fetches and parses without error here (any_success=True,
    report.failed is empty), but validation rejects every record as
    out-of-band. Mirrors the equivalent check in the pricing_intelligence
    CLI (``rate_count == 0``) -- without it, a scheduler would report success
    while history silently stops accumulating.
    """

    def blow_out_rates(bank, html):
        return re.sub(r"\d+\.\d+%", "99.99%", html)

    outcome = collect_outcome(fixture_fetcher(mutate=blow_out_rates), tmp_path)

    assert outcome.report.any_success
    assert not outcome.report.failed
    assert outcome.rate_count == 0
    assert workflow.exit_code_for(outcome) == workflow.EXIT_TOTAL_FAILURE


def test_bank_wide_rejection_is_a_partial_failure(workflow, fixture_fetcher, tmp_path):
    """One bank's rows are all implausible while the rest collect normally.

    ANZ's fetch still succeeds here (it never reaches report.failed), and the
    aggregate rate_count stays positive because the other four banks are
    unaffected -- so the plain rate_count == 0 guard alone misses this. A
    bank contributing zero accepted rows must be judged the same as an
    outright fetch failure, per-bank, not folded into the aggregate count.
    """

    def blow_out_one_bank(bank, html):
        if bank != "ANZ":
            return html
        return re.sub(r"\d+\.\d+%", "99.99%", html)

    outcome = collect_outcome(fixture_fetcher(mutate=blow_out_one_bank), tmp_path)

    assert not outcome.report.failed
    assert outcome.rate_count > 0
    assert workflow.exit_code_for(outcome) == workflow.EXIT_PARTIAL_FAILURE


def test_partial_rejection_within_a_bank_is_a_partial_failure(workflow, fixture_fetcher, tmp_path):
    """One rejected row alongside otherwise-valid rows for the same bank.

    ANZ still has plenty of accepted rates here, so the bank-wide guard
    (every row rejected) does not catch this -- a term-specific parser
    regression that corrupts a single row must still surface as a partial
    failure, not a clean exit, even though the bank as a whole "succeeded".
    """

    def blow_out_one_row(bank, html):
        if bank != "ANZ":
            return html
        return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>99.99%")

    outcome = collect_outcome(fixture_fetcher(mutate=blow_out_one_row), tmp_path)

    assert not outcome.report.failed
    assert outcome.rate_count > 0
    assert outcome.validation.rejected
    # The bank-wide-only guard from the prior round would miss this: ANZ still
    # has accepted rows, so it never appears in _validation_only_failures().
    assert not workflow._validation_only_failures(outcome)
    assert workflow.exit_code_for(outcome) == workflow.EXIT_PARTIAL_FAILURE


def test_summary_reflects_bank_wide_validation_rejection(workflow, fixture_fetcher, tmp_path):
    """A validation-only failure must be visible in the summary, not just the exit code.

    ANZ's fetch succeeds (report.failed is empty), so without this it stays
    listed under banks_succeeded and never appears in banks_failed or
    failures -- a scheduler reading the summary alone would call it healthy.
    """

    def blow_out_one_bank(bank, html):
        if bank != "ANZ":
            return html
        return re.sub(r"\d+\.\d+%", "99.99%", html)

    outcome = collect_outcome(fixture_fetcher(mutate=blow_out_one_bank), tmp_path)
    summary = workflow.build_summary(outcome)

    assert "ANZ" not in summary["banks_succeeded"]
    assert "ANZ" in summary["banks_failed"]
    anz_failures = [f for f in summary["failures"] if f["bank"] == "ANZ"]
    assert len(anz_failures) == 1
    assert anz_failures[0]["error_type"] == "validation_rejected"


def test_summary_reports_what_a_scheduler_needs(workflow, fixture_fetcher, tmp_path):
    outcome = collect_outcome(fixture_fetcher(failing_banks=["Westpac"]), tmp_path)
    summary = workflow.build_summary(outcome)

    assert set(summary["banks_succeeded"]) == {"ANZ", "ASB", "BNZ", "Kiwibank"}
    assert summary["banks_failed"] == ["Westpac"]
    assert summary["rates_accepted"] > 0
    assert summary["new_states_stored"] > 0
    assert summary["failures"][0]["bank"] == "Westpac"
    assert summary["failures"][0]["error_type"] == "ConnectionError"


def test_summary_is_json_serialisable(workflow, fixture_fetcher, tmp_path):
    """It is written to disk for the next job in a pipeline."""
    summary = workflow.build_summary(collect_outcome(fixture_fetcher(), tmp_path))

    assert json.loads(json.dumps(summary))["rates_accepted"] > 0


def test_summary_surfaces_detected_changes(workflow, fixture_fetcher, tmp_path):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(
            mutate=lambda bank, html: html.replace(
                "<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%"
            )
            if bank == "ANZ"
            else html
        )
        outcome = service.collect(persist_snapshot=False)

    summary = workflow.build_summary(outcome)
    assert summary["changes_detected"] == 1
    assert "-20 bps" in summary["changes"][0]


def test_lock_prevents_a_second_concurrent_collection(workflow, tmp_path):
    """An overrunning collection must not interleave with the next schedule."""
    lock_path = tmp_path / "collection.lock"

    with workflow._SingleInstanceLock(lock_path):
        with pytest.raises(workflow.AlreadyRunningError):
            with workflow._SingleInstanceLock(lock_path):
                pass


def test_lock_is_released_for_the_next_run(workflow, tmp_path):
    lock_path = tmp_path / "collection.lock"

    with workflow._SingleInstanceLock(lock_path):
        pass
    with workflow._SingleInstanceLock(lock_path):
        pass  # must not raise


def test_main_skips_cleanly_when_a_collection_is_already_running(workflow, tmp_path):
    lock_path = tmp_path / "collection.lock"

    with workflow._SingleInstanceLock(lock_path):
        code = workflow.main(
            ["--database", str(tmp_path / "obs.db"), "--lock-file", str(lock_path), "--quiet"]
        )

    assert code == workflow.EXIT_ALREADY_RUNNING


def test_exit_codes_are_distinct(workflow):
    codes = {
        workflow.EXIT_OK,
        workflow.EXIT_TOTAL_FAILURE,
        workflow.EXIT_PARTIAL_FAILURE,
        workflow.EXIT_ALREADY_RUNNING,
    }

    assert len(codes) == 4, "a scheduler must be able to tell these apart"


def test_run_collection_writes_a_summary_file(workflow, fixture_fetcher, tmp_path, monkeypatch):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=fixture_fetcher(),
        ),
    )
    summary_path = tmp_path / "nested" / "summary.json"

    code, summary = workflow.run_collection(
        config, render_javascript=False, summary_path=summary_path
    )

    assert code == workflow.EXIT_OK
    assert json.loads(summary_path.read_text())["rates_accepted"] == summary["rates_accepted"]


def test_config_flag_loads_pricing_intelligence_settings(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """configs/base.yaml's pricing_intelligence block must take effect here too.

    Unlike the interactive CLI (fixed earlier), this scheduler entry point had
    no --config option at all and always constructed default settings --
    selected banks, validation thresholds, collection delays, and the
    database path configured for a scheduled run would be silently ignored.
    """
    monkeypatch.chdir(tmp_path)
    custom_db = tmp_path / "from_config.db"
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=fixture_fetcher(),
        ),
    )

    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(yaml.dump(_minimal_engine_config(database_path=str(custom_db))))

    code = workflow.main(
        [
            "--config",
            str(engine_config_path),
            "--lock-file",
            str(tmp_path / "collection.lock"),
            "--quiet",
        ]
    )

    assert code == workflow.EXIT_OK
    assert custom_db.exists()
    assert not (tmp_path / "data" / "processed" / "pricing_intelligence").exists()


def test_logger_is_under_the_configured_json_logging_hierarchy(workflow):
    """configure_json_logging() only attaches its handler to "balance_pricing_engine".

    A logger outside that hierarchy silently drops INFO events (no handler)
    and sends WARNING/ERROR through logging's unformatted last-resort
    handler instead of the promised JSON -- structured log ingestion and
    monitoring would miss every scheduled-collection event.
    """
    assert workflow.logger.name.startswith("balance_pricing_engine.")

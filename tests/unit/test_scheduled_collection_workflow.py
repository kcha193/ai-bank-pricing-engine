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

from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 25, tzinfo=timezone.utc)


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

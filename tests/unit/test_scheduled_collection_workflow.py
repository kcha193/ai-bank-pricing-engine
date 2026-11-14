"""Unattended collection workflow: exit codes, locking, and run summaries.

Runs entirely against saved fixtures -- a scheduled job's behaviour must be
testable without reaching a live bank site.
"""

import importlib.util
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from balance_pricing_engine.competitor_rates.collectors import (
    ANZCollector,
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

# Collection timestamps are dated relative to "now", not pinned to a calendar
# date. Market snapshots exclude observations older than
# max_observation_age_hours (168h by default), so a fixed date here passes when
# written and starts failing silently once real time crosses that window --
# which is exactly what happened on 2026-08-27.
DAY_ONE = datetime.now(timezone.utc) - timedelta(hours=2)
DAY_TWO = datetime.now(timezone.utc) - timedelta(hours=1)


class _StubCollector:
    """Returns pre-built records instead of parsing HTML.

    Mirrors the helper of the same name in test_pricing_intelligence_service.py
    -- lets a coverage-drop scenario be built directly from records rather than
    fragile HTML-fixture surgery.
    """

    def __init__(self, bank, records, skipped_row_keys=()):
        self.bank = bank
        self._records = records
        self._skipped_row_keys = skipped_row_keys

    def collect_mortgage_rates(self, captured_at_utc=None, persist_snapshot=True):
        return BankCollectionResult(
            bank=self.bank,
            product_type=ProductType.HOME_LOAN,
            status=CollectionStatus.SUCCESS,
            source_url=f"https://www.example-bank.co.nz/{self.bank.lower()}/rates",
            records=tuple(self._records),
            skipped_row_keys=tuple(self._skipped_row_keys),
        )

    def collect_term_deposit_rates(self, captured_at_utc=None, persist_snapshot=True):
        return BankCollectionResult(
            bank=self.bank,
            product_type=ProductType.TERM_DEPOSIT,
            status=CollectionStatus.SUCCESS,
            source_url=f"https://www.example-bank.co.nz/{self.bank.lower()}/deposits",
            records=tuple(self._records),
            skipped_row_keys=tuple(self._skipped_row_keys),
        )


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


def test_summary_reflects_partial_rejection_within_a_bank(workflow, fixture_fetcher, tmp_path):
    """A summary consumer must be able to identify which bank had a rejected row.

    ANZ has plenty of accepted rates here, so neither the zero-accepted-rows
    nor the low-coverage bank-scoped check flags it -- yet exit_code_for()
    already reports this run as a partial failure via validation.rejected.
    Without _partial_rejection_failures(), the summary simultaneously showed
    rates_rejected > 0 while listing ANZ under banks_succeeded with no
    bank-specific failures entry to point an operator at.
    """

    def blow_out_one_row(bank, html):
        if bank != "ANZ":
            return html
        return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>99.99%")

    outcome = collect_outcome(fixture_fetcher(mutate=blow_out_one_row), tmp_path)
    summary = workflow.build_summary(outcome)

    assert summary["rates_rejected"] > 0
    assert "ANZ" not in summary["banks_succeeded"]
    assert "ANZ" in summary["banks_failed"]
    anz_failures = [f for f in summary["failures"] if f["bank"] == "ANZ"]
    assert len(anz_failures) == 1
    assert anz_failures[0]["error_type"] == "validation_rejected"


def test_bootstrap_run_with_only_non_comparable_rates_is_a_total_failure(
    workflow, make_record, tmp_path
):
    """A bootstrap run whose only accepted rows are all non_comparable (e.g.
    managed-fund returns, PIE effective rates) must not report success.

    Those rows are genuinely validation-accepted (rate_count > 0), so the
    plain rate_count == 0 guard misses this -- but pricing_intelligence.snapshot
    excludes every non_comparable record from a market snapshot, so the
    usable market view this run actually produced is empty. With no prior
    baseline (a bootstrap run), low_coverage_banks stays empty too: there is
    nothing to compare coverage against yet. Only a comparable-rate-count
    check catches this."""
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    non_comparable_record = make_record(bank="ANZ", rate_category="non_comparable")
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        collectors=[_StubCollector("ANZ", [non_comparable_record])],
    ) as service:
        outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    assert not outcome.report.failed
    assert not outcome.validation.rejected
    assert outcome.rate_count > 0
    assert not outcome.low_coverage_banks
    assert workflow._comparable_rate_count(outcome) == 0
    assert workflow.exit_code_for(outcome) == workflow.EXIT_TOTAL_FAILURE


def test_bank_with_only_non_comparable_rates_is_a_partial_failure_among_others(
    workflow, make_record, tmp_path
):
    """Scoped to one bank: ANZ's accepted rows are all non_comparable while
    ASB collects normally, so the run as a whole has comparable rates and
    must not be a total failure -- but ANZ contributed nothing usable and
    must still be flagged, not silently folded into banks_succeeded."""
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        collectors=[
            _StubCollector("ANZ", [make_record(bank="ANZ", rate_category="non_comparable")]),
            _StubCollector("ASB", [make_record(bank="ASB")]),
        ],
    ) as service:
        outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    assert workflow._comparable_rate_count(outcome) > 0
    assert "ANZ" in workflow._banks_with_zero_comparable_rates(outcome)
    assert workflow.exit_code_for(outcome) == workflow.EXIT_PARTIAL_FAILURE

    summary = workflow.build_summary(outcome)
    assert "ANZ" not in summary["banks_succeeded"]
    assert "ASB" in summary["banks_succeeded"]
    anz_failures = [f for f in summary["failures"] if f["bank"] == "ANZ"]
    assert len(anz_failures) == 1
    assert anz_failures[0]["error_type"] == "no_comparable_rates"


def test_bootstrap_deposit_run_with_only_pie_rates_is_a_total_failure(
    workflow, make_record, tmp_path
):
    """A term-deposit bootstrap run whose parser only matches the PIE table
    (rate_category="pie_term_deposit") while missing every ordinary
    term_deposit row must not report success.

    pie_term_deposit is a real, validation-accepted category -- not
    non_comparable -- so the round-42 fix (which only excluded
    non_comparable) is not enough on its own: the service's default deposit
    snapshot filters to rate_category="term_deposit" specifically, so these
    PIE rows can never enter it. The comparable-rate check must resolve the
    product's actual default category, not just exclude non_comparable."""
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    pie_record = make_record(
        bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="pie_term_deposit"
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        collectors=[_StubCollector("ANZ", [pie_record])],
    ) as service:
        outcome = service.collect(
            captured_at_utc=DAY_ONE, product_type=ProductType.TERM_DEPOSIT, persist_snapshot=False
        )

    assert not outcome.report.failed
    assert not outcome.validation.rejected
    assert outcome.rate_count > 0
    assert not outcome.low_coverage_banks
    assert workflow._comparable_rate_count(outcome, ProductType.TERM_DEPOSIT) == 0
    assert (
        workflow.exit_code_for(outcome, ProductType.TERM_DEPOSIT) == workflow.EXIT_TOTAL_FAILURE
    )


def test_low_coverage_bank_is_a_partial_failure(workflow, make_record, tmp_path):
    """A page-layout regression that leaves one row parseable while silently
    dropping the rest of a bank's table.

    ANZ's fetch succeeds and produces one accepted, unrejected rate here --
    neither report.failed nor validation.rejected catches this, since the
    missing rows were never parsed at all rather than parsed-and-rejected.
    Only the coverage check (matched vs. previously known key count) can,
    which the service already computes for withdrawal-suppression purposes
    (CollectionOutcome.low_coverage_banks) but the workflow did not consume.
    """
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    baseline_records = [
        make_record(bank="ANZ", term_months=months, rate_pct=4.79 + months / 100)
        for months in (12, 24, 36, 60)
    ]
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        collectors=[_StubCollector("ANZ", baseline_records)],
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

        # Second run: only 1 of the 4 previously known terms is present --
        # 25% coverage, below the default 50% min_bank_coverage_ratio.
        surviving_row = baseline_records[0].model_copy(update={"captured_at_utc": DAY_TWO})
        service._collectors = [_StubCollector("ANZ", [surviving_row])]
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert not outcome.report.failed
    assert not outcome.validation.rejected
    assert outcome.rate_count > 0
    assert "ANZ" in outcome.low_coverage_banks
    assert workflow.exit_code_for(outcome) == workflow.EXIT_PARTIAL_FAILURE

    summary = workflow.build_summary(outcome)
    assert "ANZ" not in summary["banks_succeeded"]
    assert "ANZ" in summary["banks_failed"]
    anz_failures = [f for f in summary["failures"] if f["bank"] == "ANZ"]
    assert len(anz_failures) == 1
    assert anz_failures[0]["error_type"] == "low_coverage"


def test_hard_skipped_row_is_a_partial_failure(workflow, make_record, tmp_path):
    """A row that fails CompetitorRateRecord construction outright (see
    parse_html_rates()'s skipped_row_keys) never reaches
    validate_observations() at all, so it shows up in neither report.failed
    nor validation.rejected. On a bootstrap run there is not even a stored
    predecessor for the row to fall back on -- without surfacing this,
    exit_code_for() could return success while a market-derived snapshot and
    pricing assumption were built from an incomplete bank range."""
    from balance_pricing_engine.competitor_rates.models import category_agnostic_key

    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    skipped_key = category_agnostic_key(
        "ANZ", ProductType.HOME_LOAN, 12, "1 year", "unknown", None, None
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        collectors=[
            _StubCollector(
                "ANZ", [make_record(bank="ANZ")], skipped_row_keys=[skipped_key]
            )
        ],
    ) as service:
        outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    assert not outcome.report.failed
    assert not outcome.validation.rejected
    assert outcome.rate_count > 0
    assert outcome.report.skipped_row_keys
    assert workflow.exit_code_for(outcome) == workflow.EXIT_PARTIAL_FAILURE

    summary = workflow.build_summary(outcome)
    assert summary["rows_skipped"] == 1
    assert "ANZ" not in summary["banks_succeeded"]
    assert "ANZ" in summary["banks_failed"]
    anz_failures = [f for f in summary["failures"] if f["bank"] == "ANZ"]
    assert len(anz_failures) == 1
    assert anz_failures[0]["error_type"] == "row_skipped"


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


def test_product_flag_lets_the_workflow_collect_term_deposits(
    workflow, tmp_path, monkeypatch
):
    """Without --product, run_collection() always defaulted to home_loan, so
    a scheduled deployment never refreshed term-deposit observations no
    matter how it was configured. product_type must actually reach
    service.collect() and produce a deposit-scoped summary."""
    fixtures = Path("tests/fixtures/pricing_intelligence")
    term_deposit_html = (fixtures / "anz_term_deposits.html").read_bytes()

    def fetch(url: str) -> bytes:
        assert url == ANZCollector.term_deposit_url
        return term_deposit_html

    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        selected_banks=["ANZ"],
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fetch
        ),
    )

    code, summary = workflow.run_collection(
        config, render_javascript=False, product_type=ProductType.TERM_DEPOSIT
    )

    assert code == workflow.EXIT_OK
    assert summary["product_type"] == "term_deposit"
    assert summary["rates_accepted"] > 0


def test_run_collection_logs_partial_failure_at_warning(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """In the launchd deployment, this log line is the only structured health
    event -- alerting that filters on warning/error severity must not miss an
    incomplete market view just because exit_code_for() treats it as a
    failure while the completion event was logged at INFO.

    A directly-attached handler is used instead of caplog: configure_json_logging()
    (called by an earlier test's workflow.main() in this same process) sets
    ``propagate = False`` on the balance_pricing_engine logger hierarchy,
    which stops records from reaching caplog's root-based capture.
    """
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
            fetcher=fixture_fetcher(failing_banks=["Westpac"]),
        ),
    )

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    workflow.logger.addHandler(handler)
    try:
        code, _ = workflow.run_collection(config, render_javascript=False)
    finally:
        workflow.logger.removeHandler(handler)

    assert code == workflow.EXIT_PARTIAL_FAILURE
    finished_record = next(r for r in records if r.getMessage() == "market collection finished")
    assert finished_record.levelname == "WARNING"


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


def test_config_flag_preserves_env_db_path_when_yaml_omits_it(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """A --config whose pricing_intelligence block omits database_path must not
    discard PRICING_INTELLIGENCE_DB_PATH in favour of PricingIntelligenceConfig()'s
    hard-coded default -- same fix as the interactive CLI's config-loading
    branch, applied to this scheduler entry point's equivalent branch."""
    monkeypatch.chdir(tmp_path)
    env_db = tmp_path / "from_env.db"
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(env_db))
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
    engine_config_path.write_text(yaml.dump(_minimal_engine_config(selected_banks=["ANZ"])))

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
    assert env_db.exists()
    assert not (tmp_path / "data" / "processed" / "pricing_intelligence").exists()


def test_logger_is_under_the_configured_json_logging_hierarchy(workflow):
    """configure_json_logging() only attaches its handler to "balance_pricing_engine".

    A logger outside that hierarchy silently drops INFO events (no handler)
    and sends WARNING/ERROR through logging's unformatted last-resort
    handler instead of the promised JSON -- structured log ingestion and
    monitoring would miss every scheduled-collection event.
    """
    assert workflow.logger.name.startswith("balance_pricing_engine.")


# ------------------------------------------------------- webhook alert delivery


def _reprice_fetcher(fixture_fetcher):
    """A fetcher where ANZ's 1-year special has moved -20 bps."""
    return fixture_fetcher(
        mutate=lambda bank, html: html.replace(
            "<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%"
        )
        if bank == "ANZ"
        else html
    )


def test_a_reprice_is_delivered_to_a_configured_webhook(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x"
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    captured = []

    def fake_urlopen(request, timeout=None):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        captured.append((request.full_url, json.loads(request.data)))
        return FakeResponse()

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=_reprice_fetcher(fixture_fetcher),
        ),
    )

    code, summary = workflow.run_collection(config, render_javascript=False)

    assert code == workflow.EXIT_OK
    assert summary["changes_detected"] == 1
    assert summary["alerts_sent"] == 1
    assert len(captured) == 1
    url, payload = captured[0]
    assert url == "https://hooks.example/x"
    assert payload["bank"] == "ANZ"
    assert payload["change_bps"] == -20.0
    assert "text" in payload


def test_no_webhook_configured_sends_nothing(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """The default state: no delivery channel, nothing breaks."""
    monkeypatch.delenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", raising=False)
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    called = []
    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module, "urlopen", lambda *a, **k: called.append(1) or (_ for _ in ()).throw(
            AssertionError("urlopen must never be called with no webhook configured")
        )
    )
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=_reprice_fetcher(fixture_fetcher),
        ),
    )

    code, summary = workflow.run_collection(config, render_javascript=False)

    assert code == workflow.EXIT_OK
    assert summary["changes_detected"] == 1
    assert summary["alerts_sent"] == 0
    assert called == []


def test_a_broken_webhook_does_not_fail_the_collection(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """A collection failure and a delivery failure must stay independent."""
    monkeypatch.setenv(
        "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://dead.example/x"
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    def broken_urlopen(request, timeout=None):
        raise ConnectionError("webhook host unreachable")

    monkeypatch.setattr(alerts_module, "urlopen", broken_urlopen)
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=_reprice_fetcher(fixture_fetcher),
        ),
    )

    code, summary = workflow.run_collection(config, render_javascript=False)

    assert code == workflow.EXIT_OK, "collection succeeded; delivery failure must not change that"
    assert summary["changes_detected"] == 1
    assert summary["alerts_sent"] == 0


def test_a_change_that_is_not_a_rate_move_is_not_pushed_to_the_webhook(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """New/removed products are material changes but not rate moves; alerts
    are currently scoped to rate moves, matching the CLI's own --alerts path."""
    monkeypatch.setenv(
        "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x"
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    called = []
    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module,
        "urlopen",
        lambda *a, **k: called.append(1),
    )
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            # Westpac withdrawn entirely: REMOVED_PRODUCT events, no rate moves.
            fetcher=fixture_fetcher(failing_banks=["Westpac"]),
        ),
    )

    code, summary = workflow.run_collection(config, render_javascript=False)

    assert summary["alerts_sent"] == 0
    assert called == [], "a withdrawal must not be pushed as a rate-move alert"


def test_webhook_text_field_is_threaded_through_the_cli(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://discord.example/x"
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    captured = []
    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    def fake_urlopen(request, timeout=None):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        captured.append(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=_reprice_fetcher(fixture_fetcher),
        ),
    )

    code = workflow.main(
        [
            "--database",
            str(tmp_path / "obs.db"),
            "--lock-file",
            str(tmp_path / "collection.lock"),
            "--quiet",
            "--webhook-text-field",
            "content",
        ]
    )

    assert code == workflow.EXIT_OK
    assert "content" in captured[0]
    assert "text" not in captured[0]


@pytest.mark.parametrize("collection_exit", [0, 1, 2, 3])
@pytest.mark.parametrize("product", ["home_loan", "term_deposit"])
def test_canary_health_status_exposes_partial_collection_failures(collection_exit, product):
    """Run the actual health-report shell step after GitHub resolves its output."""
    workflow = yaml.safe_load(Path(".github/workflows/collector-canary.yml").read_text())
    steps = next(iter(workflow["jobs"].values()))["steps"]
    report_step = next(step for step in steps if step.get("name") == "Report collector health")
    script = report_step["run"].replace(
        "${{ steps.collect.outputs.exit_code }}", str(collection_exit)
    ).replace("${{ matrix.product }}", product)
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.stderr == ""
    assert result.returncode == (0 if collection_exit == 0 else 1), result.stdout
    if collection_exit == 0:
        assert product in result.stdout
    else:
        assert "::error::" in result.stdout


def test_a_backwards_clock_is_a_classified_failure_not_a_traceback(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """The store rejects a write older than the product's latest run.

    ``service.collect()`` stamps its own ``datetime.now``, so an NTP
    correction, a VM snapshot restore, or any backwards clock step makes that
    guard fire on an ordinary scheduled run. An unattended worker must report
    that as a classified total failure with a summary the canary can upload,
    not die with an unhandled traceback.
    """
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fixture_fetcher()
        ),
    )
    # Establish a run, then wind the clock back behind it.
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_TWO + timedelta(days=1), persist_snapshot=False)

    summary_path = tmp_path / "summary.json"
    # A directly-attached handler, not caplog: configure_json_logging() sets
    # propagate = False on the balance_pricing_engine hierarchy, so records
    # never reach caplog's root-based capture once another test has run it.
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    workflow.logger.addHandler(handler)
    try:
        code, summary = workflow.run_collection(
            config, render_javascript=False, summary_path=summary_path
        )
    finally:
        workflow.logger.removeHandler(handler)

    assert code == workflow.EXIT_TOTAL_FAILURE
    assert summary["rates_accepted"] == 0
    assert summary["banks_succeeded"] == []
    assert summary["banks_failed"]
    assert json.loads(summary_path.read_text())["failures"][0]["error_type"] == (
        "ChronologicalOrderError"
    )
    aborted = [
        record
        for record in records
        if record.getMessage() == "market collection aborted"
        and record.levelno == logging.ERROR
    ]
    assert len(aborted) == 1
    assert "chronological" in aborted[0].structured_fields["reason"]
    assert aborted[0].structured_fields["exit_code"] == workflow.EXIT_TOTAL_FAILURE


def test_a_backwards_clock_is_caught_before_fetching_any_bank(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """service.collect() fetches and validates every bank before its
    persistence step reaches record_observations()'s chronological-order
    guard, so by the time that guard raises, the fetch work is already done
    and its results (report, validation) are thrown away with the
    exception -- the synthesized failure summary then has no real per-bank
    data to report even if, say, a parser had also failed on some other
    bank in that same attempt. Checking the clock before fetching (not just
    relying on the later guard) means a backwards clock is caught for free,
    with no fetch attempted at all."""
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    fetch_calls = []

    def counting_fetcher():
        fetch = fixture_fetcher()

        def fetch_and_count(url):
            fetch_calls.append(url)
            return fetch(url)

        return fetch_and_count

    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=counting_fetcher()
        ),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_TWO + timedelta(days=1), persist_snapshot=False)

    code, _ = workflow.run_collection(config, render_javascript=False)

    assert code == workflow.EXIT_TOTAL_FAILURE
    assert fetch_calls == []


# ---------------------------------------------------- scenario-trigger wiring


def test_a_reprice_auto_triggers_pricing_scenarios_end_to_end(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """The full wiring: --config supplies both pricing_intelligence settings
    and the economic assumptions scenario-triggering needs; a real (not
    injected/faked) run_triggered_scenarios() call runs against the small
    engine config, and its result is both persisted and appended to the
    delivered alert."""
    monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.chdir(tmp_path)
    custom_db = tmp_path / "obs.db"

    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=str(custom_db), collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(custom_db),
        fetcher=fixture_fetcher(),
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    captured = []

    def fake_urlopen(request, timeout=None):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        captured.append(json.loads(request.data))
        return FakeResponse()

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=_reprice_fetcher(fixture_fetcher),
        ),
    )

    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(_minimal_engine_config(database_path=str(custom_db)))
    )

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
    assert len(captured) == 1
    assert "PRICING ENGINE" in captured[0]["text"]
    assert "Synthetic decision support only" in captured[0]["text"]
    assert "Status: REVIEW REQUIRED" in captured[0]["text"]

    with ObservationStore(custom_db) as store:
        latest = store.get_latest_run_record()
        runs = store.get_scenario_runs(latest.run_id)
    assert len(runs) == 1
    assert runs[0]["term_months"] == 12


def test_scenario_trigger_fn_isolates_a_failing_term_from_the_others(
    workflow, fixture_fetcher, tmp_path, monkeypatch
):
    """When several terms trigger together, one term's scenario computation
    failing (e.g. no external competitor peer left once our_bank is
    excluded -- a documented reachable case, not a bug) must not discard
    every other term's already-computed comparison. orchestration's own
    catch around the whole callback (see its docstring) only stops a
    failure from aborting collection -- it doesn't recover per-term results,
    so this callback must isolate per term itself."""
    monkeypatch.chdir(tmp_path)
    custom_db = tmp_path / "obs.db"

    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=str(custom_db), collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(custom_db),
        fetcher=fixture_fetcher(),
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        snapshot = service.get_market_snapshot()

    engine_config_path = tmp_path / "engine.yaml"
    engine_config_path.write_text(
        yaml.dump(_minimal_engine_config(database_path=str(custom_db)))
    )

    class _StopEarly(Exception):
        pass

    captured_fn = {}

    def fake_run_operational_cycle(service, **kwargs):
        captured_fn["scenario_trigger_fn"] = kwargs["scenario_trigger_fn"]
        raise _StopEarly()

    monkeypatch.setattr(workflow, "run_operational_cycle", fake_run_operational_cycle)
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fixture_fetcher()
        ),
    )

    # Patched before main() runs: scenario_trigger_fn's `from ... import
    # run_triggered_scenarios` executes inside run_collection() itself
    # (during main()), binding whatever this module attribute resolves to
    # at that moment -- patching afterwards would be too late to affect the
    # closure already captured below.
    import balance_pricing_engine.pricing_scenario_trigger as trigger_module

    real_run_triggered_scenarios = trigger_module.run_triggered_scenarios

    def flaky_run_triggered_scenarios(base_config, *, term_months, snapshot, **kwargs):
        if term_months == 12:
            raise RuntimeError("simulated: no external peer remains for this term")
        return real_run_triggered_scenarios(
            base_config, term_months=term_months, snapshot=snapshot, **kwargs
        )

    monkeypatch.setattr(
        trigger_module, "run_triggered_scenarios", flaky_run_triggered_scenarios
    )

    try:
        workflow.main(
            [
                "--config",
                str(engine_config_path),
                "--lock-file",
                str(tmp_path / "collection.lock"),
                "--quiet",
            ]
        )
    except _StopEarly:
        pass

    scenario_trigger_fn = captured_fn["scenario_trigger_fn"]
    assert scenario_trigger_fn is not None

    results = scenario_trigger_fn([12, 24], snapshot)

    assert 12 not in results, "the failing term must be skipped, not raised"
    assert 24 in results, "an unaffected term's comparison must still be returned"


def test_no_config_means_no_scenario_trigger(workflow, fixture_fetcher, tmp_path, monkeypatch):
    """--database alone (no --config) has no economic assumptions to run
    scenarios against, so scenario-triggering is silently skipped, not an
    error."""
    monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    custom_db = tmp_path / "obs.db"

    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=str(custom_db), collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(custom_db),
        fetcher=fixture_fetcher(),
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    captured = []

    def fake_urlopen(request, timeout=None):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        captured.append(json.loads(request.data))
        return FakeResponse()

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"],
            store=kwargs["store"],
            fetcher=_reprice_fetcher(fixture_fetcher),
        ),
    )

    code = workflow.main(
        [
            "--database",
            str(custom_db),
            "--lock-file",
            str(tmp_path / "collection.lock"),
            "--quiet",
        ]
    )

    assert code == workflow.EXIT_OK
    assert len(captured) == 1
    assert "PRICING ENGINE" not in captured[0]["text"]

    with ObservationStore(custom_db) as store:
        latest = store.get_latest_run_record()
        assert store.get_scenario_runs(latest.run_id) == []

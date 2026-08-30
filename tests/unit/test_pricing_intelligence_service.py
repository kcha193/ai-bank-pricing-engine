"""End-to-end service behaviour, driven entirely by saved fixtures."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.collectors import BankCollectionResult, CollectionStatus
from balance_pricing_engine.competitor_rates.models import ProductType, category_agnostic_key
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange, detect_changes
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.market_state import MarketState
from balance_pricing_engine.pricing_intelligence.service import (
    PricingIntelligenceService,
    _resolve_rate_category,
    _resolve_suspicious,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import CompetitorRateStrategy

# Collection timestamps are dated relative to "now", not pinned to a calendar
# date. Market snapshots exclude observations older than
# max_observation_age_hours (168h by default), so a fixed date here passes when
# written and starts failing silently once real time crosses that window --
# which is exactly what happened on 2026-08-27.
#
# Offsets are whole days (not hours) so DAY_ONE and DAY_TWO always land on
# distinct calendar dates regardless of what UTC hour the suite runs at --
# get_rate_history_series() collapses observations by captured_at_utc.date(),
# so same-day anchors would collapse the two-date history reconstructions these
# tests exercise.
DAY_ONE = datetime.now(timezone.utc) - timedelta(days=2)
DAY_TWO = datetime.now(timezone.utc) - timedelta(days=1)
DAY_THREE = DAY_ONE + timedelta(days=2)


def build_service(fetcher, **config_overrides):
    config = PricingIntelligenceConfig(
        database_path=":memory:",
        # The inter-request politeness delay is right for real bank sites but
        # would add seconds per collection to the suite; fixtures need none.
        collection=CollectionConfig(inter_request_delay_seconds=0),
        **config_overrides,
    )
    return PricingIntelligenceService(
        config=config, store=ObservationStore(":memory:"), fetcher=fetcher
    )


class _StubCollector:
    """Returns pre-built records instead of parsing HTML.

    Every real per-bank parser derives ``product_name`` from the same table
    cell that determines ``rate_category`` (see ``extractors._product_name``),
    so an HTML-fixture edit can't flip one without the other. This isolates a
    pure rate_category flip -- the exact scenario the category-agnostic
    fallback exists for -- while going through the real collect() pipeline.
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


def cut_anz_one_year(bank, html):
    if bank == "ANZ":
        return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
    return html


@pytest.fixture
def service(fixture_fetcher):
    with build_service(fixture_fetcher()) as instance:
        yield instance


def test_a_collection_run_stores_every_bank(service):
    outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    assert len(outcome.report.failed) == 0
    assert set(outcome.succeeded_banks) == {"ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"}
    assert outcome.rate_count > 20
    assert outcome.write_result.inserted == outcome.rate_count


def test_the_first_collection_reports_no_changes(service):
    """Everything being new is not market intelligence."""
    outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    assert outcome.changes == ()


def test_new_products_are_reported_after_the_market_goes_fully_quiet(fixture_fetcher):
    """An empty baseline from every product being withdrawn is not a first collection."""
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)  # genuine first run

        # Confirm every currently-known key withdrawn, directly via the store,
        # so the baseline is empty afterwards without this being a first run.
        baseline = service.store.get_current_observations()
        withdrawal = detect_changes(
            previous=baseline,
            current=[],
            detected_at=DAY_TWO,
            banks_collected=[record.bank for record in baseline],
        )
        service.store.record_changes(withdrawal)
        assert service.store.get_current_observations() == [], "sanity check"

        outcome = service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)

    new_products = [
        change for change in outcome.changes if change.change_type is ChangeType.NEW_PRODUCT
    ]
    assert len(new_products) > 0


def test_a_bank_succeeding_for_the_first_time_is_not_reported_as_new_products(make_record):
    """A bank that failed (or was unreachable) every prior run and only now
    succeeds must not report a burst of new-product events just because some
    other bank already has a long baseline -- bootstrap suppression is per
    bank, not global."""
    anz_records = [make_record(bank="ANZ", rate_pct=4.79)]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", anz_records)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)  # ANZ's genuine first run

    bnz_records = [make_record(bank="BNZ", rate_pct=4.85)]
    service._collectors = [
        _StubCollector("ANZ", anz_records),
        _StubCollector("BNZ", bnz_records),
    ]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    bnz_new_products = [
        change
        for change in outcome.changes
        if change.change_type is ChangeType.NEW_PRODUCT and change.bank == "BNZ"
    ]
    assert bnz_new_products == [], "BNZ's first-ever success must be suppressed as bootstrap"


def test_an_unchanged_second_collection_produces_no_changes(service):
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert outcome.changes == ()
    assert outcome.write_result.inserted == 0
    assert outcome.write_result.unchanged > 0


def test_a_real_reprice_produces_a_change_event(fixture_fetcher):
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert len(outcome.changes) == 1
    change = outcome.changes[0]
    assert change.bank == "ANZ"
    assert change.change_type is ChangeType.RATE_DECREASE
    assert change.change_bps == -20.0


def test_one_failing_bank_does_not_fail_the_run(fixture_fetcher):
    with build_service(fixture_fetcher(failing_banks=["Westpac"])) as service:
        outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        stored_banks = {record.bank for record in service.store.get_latest_rates()}

    assert outcome.failed_banks == ("Westpac",)
    assert len(outcome.succeeded_banks) == 4
    assert outcome.rate_count > 0
    assert "Westpac" not in stored_banks


def test_a_bank_going_offline_is_not_reported_as_a_withdrawal(fixture_fetcher):
    """The regression that matters most: a fetch failure is not a market event."""
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(failing_banks=["Westpac"])
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    withdrawals = [
        change for change in outcome.changes if change.change_type is ChangeType.REMOVED_PRODUCT
    ]
    assert withdrawals == []


def test_a_bank_recovering_from_a_transient_failure_is_not_reported_as_new(fixture_fetcher):
    """A bank that missed one run and returns unchanged must not look brand new."""
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(failing_banks=["Westpac"])
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        service._fetcher = fixture_fetcher()
        outcome = service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)

    new_products = [
        change for change in outcome.changes if change.change_type is ChangeType.NEW_PRODUCT
    ]
    assert new_products == []


def test_a_genuine_withdrawal_is_still_reported(fixture_fetcher):
    def withdraw_asb_five_year(bank, html):
        if bank == "ASB":
            return html.replace(
                "<tr><td>Fixed Special</td><td>5 years</td><td>5.75% p.a.</td></tr>", ""
            )
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=withdraw_asb_five_year)
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    withdrawals = [
        change for change in outcome.changes if change.change_type is ChangeType.REMOVED_PRODUCT
    ]
    assert len(withdrawals) == 1
    assert withdrawals[0].bank == "ASB"
    assert withdrawals[0].term_months == 60


def test_a_withdrawal_is_not_re_reported_on_a_later_collection(fixture_fetcher):
    """Once recorded, a withdrawal must not be re-detected every subsequent run."""

    def withdraw_asb_five_year(bank, html):
        if bank == "ASB":
            return html.replace(
                "<tr><td>Fixed Special</td><td>5 years</td><td>5.75% p.a.</td></tr>", ""
            )
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=withdraw_asb_five_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        outcome = service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)

    withdrawals = [
        change for change in outcome.changes if change.change_type is ChangeType.REMOVED_PRODUCT
    ]
    assert withdrawals == []


def test_a_partial_parse_regression_is_not_reported_as_a_mass_withdrawal(fixture_fetcher):
    """A page-layout regression leaving one parseable row is not six withdrawals."""

    def strip_asb_to_one_row(bank, html):
        if bank != "ASB":
            return html
        return re.sub(
            r"<tbody>.*?</tbody>",
            "<tbody><tr><td>Fixed Special</td><td>1 year</td><td>4.89% p.a.</td></tr></tbody>",
            html,
            flags=re.DOTALL,
        )

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=strip_asb_to_one_row)
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    asb_withdrawals = [
        change
        for change in outcome.changes
        if change.change_type is ChangeType.REMOVED_PRODUCT and change.bank == "ASB"
    ]
    assert asb_withdrawals == []


def test_bogus_rows_matching_the_baseline_count_are_not_a_reliable_collection(make_record):
    """Coverage must be measured by matched predecessor identity, not a raw
    accepted-record count: a parser regression that replaces missing rows
    with the same number of plausible but wrongly-identified ones would
    otherwise satisfy the baseline count while matching none of the bank's
    actual known products, still reading all of them as withdrawn."""
    baseline = [
        make_record(bank="ANZ", term_months=term, product_name=f"Fixed {term}m")
        for term in (6, 12, 18, 24)
    ]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", baseline)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    # Same row count as the baseline, but none share an identity with it --
    # a stand-in for a parser fault that garbles term/product fields.
    garbled = [
        make_record(bank="ANZ", term_months=term, product_name=f"Garbled {term}m")
        for term in (36, 48, 60, 84)
    ]
    service._collectors = [_StubCollector("ANZ", garbled)]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    anz_withdrawals = [
        change
        for change in outcome.changes
        if change.change_type is ChangeType.REMOVED_PRODUCT and change.bank == "ANZ"
    ]
    assert anz_withdrawals == []


def test_non_comparable_rows_do_not_count_against_coverage(make_record):
    """A bank whose non_comparable (audit-only) rows fail to (re-)extract
    must not be read as low-coverage from that alone: coverage is measured
    over comparable rows only, or a bank whose non_comparable rows outnumber
    its comparable ones would be permanently excluded from withdrawal
    detection even though every comparable rate collected cleanly -- and
    because that exclusion is permanent, those stale non_comparable rows
    would never get pruned either."""
    comparable = make_record(bank="ANZ", term_months=12, product_name="Fixed 12m")
    non_comparable = [
        make_record(bank="ANZ", rate_category="non_comparable", product_name=f"PIE Fund {i}")
        for i in range(5)
    ]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [comparable] + non_comparable)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    # This run: only the comparable row is re-collected -- a stand-in for a
    # parser fault (or a page change) that stops extracting just the
    # audit-only rows while every genuine advertised rate collects cleanly.
    service._collectors = [_StubCollector("ANZ", [comparable])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert "ANZ" not in outcome.low_coverage_banks


def test_non_comparable_rows_missing_coverage_does_not_emit_withdrawals(make_record):
    """The other half of the guard test_non_comparable_rows_do_not_count_against_coverage
    checks: a bank whose non_comparable extraction alone regresses still
    counts as reliably collected (comparable coverage is fine), so
    detect_changes() would otherwise read every now-missing non_comparable
    identity as a genuine REMOVED_PRODUCT -- persisting the exact
    parser-failure scenario as change events instead of merely dropping the
    coverage ratio the way _reliably_collected_banks() already does for a
    comparable-coverage regression."""
    comparable = make_record(bank="ANZ", term_months=12, product_name="Fixed 12m")
    non_comparable = [
        make_record(bank="ANZ", rate_category="non_comparable", product_name=f"PIE Fund {i}")
        for i in range(5)
    ]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [comparable] + non_comparable)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    # Same stand-in as the coverage test: only the comparable row re-collects.
    service._collectors = [_StubCollector("ANZ", [comparable])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    non_comparable_withdrawals = [
        change
        for change in outcome.changes
        if change.change_type is ChangeType.REMOVED_PRODUCT
        and change.rate_category == "non_comparable"
    ]
    assert non_comparable_withdrawals == []


def test_a_validation_rejected_record_is_not_reported_as_a_withdrawal(fixture_fetcher):
    """A single implausible rate must not read as the bank withdrawing that line."""

    def corrupt_kiwibank_three_year(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>55.35%")
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=corrupt_kiwibank_three_year)
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert outcome.validation.rejected, "sanity check: the record really was rejected"
    withdrawals = [
        change for change in outcome.changes if change.change_type is ChangeType.REMOVED_PRODUCT
    ]
    assert withdrawals == []


def test_a_hard_skipped_row_is_not_reported_as_a_withdrawal(make_record):
    """A row that fails CompetitorRateRecord construction outright during
    extraction -- before it can even reach validate_observations() to land in
    validation.rejected -- must not read as its predecessor having been
    withdrawn, the same protection test_a_validation_rejected_record_is_not_
    reported_as_a_withdrawal gives a row that fails validation afterwards."""
    baseline = [
        make_record(bank="ANZ", term_months=12, product_name="Fixed 12m"),
        make_record(bank="ANZ", term_months=24, product_name="Fixed 24m"),
    ]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", baseline)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    # This run: the 12m row parsed fine, but the 24m row failed
    # CompetitorRateRecord construction entirely -- represented here exactly
    # as parse_html_rates() would report it, via its own skipped_row_keys
    # parameter (see test_competitor_rate_extractors.py for that unit test).
    surviving = [make_record(bank="ANZ", term_months=12, product_name="Fixed 12m")]
    skipped_key = category_agnostic_key(
        bank="ANZ",
        product_type=ProductType.HOME_LOAN,
        term_months_normalised=24,
        term_label_raw="24 months",
        interest_payment_frequency="unknown",
        min_balance_or_deposit=None,
        max_balance_or_deposit=None,
    )
    service._collectors = [_StubCollector("ANZ", surviving, skipped_row_keys=[skipped_key])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    anz_withdrawals = [
        change
        for change in outcome.changes
        if change.change_type is ChangeType.REMOVED_PRODUCT and change.bank == "ANZ"
    ]
    assert anz_withdrawals == []


def test_a_validation_rejected_line_keeps_its_last_known_rate_in_the_snapshot(fixture_fetcher):
    """A rejected line must not silently disappear from market-derived pricing either."""

    def corrupt_kiwibank_three_year(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>55.35%")
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=corrupt_kiwibank_three_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        snapshot = service.get_market_snapshot(now=DAY_TWO)

    assert snapshot.term(36).rate_for("Kiwibank") == 5.35


def test_a_broken_parser_is_flagged_suspicious_not_silently_accepted(fixture_fetcher):
    def break_kiwibank(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>8.49%")
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=break_kiwibank)
        outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        state = service.get_market_state(now=DAY_TWO)

    suspicious = [change for change in outcome.changes if change.suspicious]
    assert len(suspicious) == 1
    assert suspicious[0].change_bps == 314.0
    assert state.state is MarketState.LOW, "a parser artefact must not fake market activity"


def test_market_state_ignores_changes_outside_the_configured_market():
    """A change for a bank or term outside selected_banks/tracked_terms_months
    must not push the classification, even if the database holds it from an
    earlier or broader configuration -- the snapshot is already scoped to
    those settings, so market state must match."""
    config = PricingIntelligenceConfig(
        database_path=":memory:",
        selected_banks=["ANZ"],
        tracked_terms_months=[12],
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    service = PricingIntelligenceService(config=config, store=ObservationStore(":memory:"))
    out_of_scope_change = RateChange(
        observation_key="BNZ|home_loan|special|24|unknown|fixed special",
        bank="BNZ",
        product_type=ProductType.HOME_LOAN,
        product_name="Fixed Special",
        rate_category="special",
        term_months=24,
        change_type=ChangeType.RATE_DECREASE,
        detected_at=DAY_ONE,
        previous_rate_pct=5.00,
        current_rate_pct=4.50,
        change_bps=-50.0,
    )
    service.store.record_changes([out_of_scope_change])

    state = service.get_market_state(now=DAY_ONE)

    assert state.state is MarketState.LOW, "the change is outside the configured market"


def test_get_changes_scope_to_market_filters_by_selected_banks_and_terms():
    """The same scope get_market_state() and every snapshot already use,
    available directly to callers like the dashboard's "Recent repricing"
    section that read get_changes() without their own bank/term filter."""
    config = PricingIntelligenceConfig(
        database_path=":memory:",
        selected_banks=["ANZ"],
        tracked_terms_months=[12],
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    service = PricingIntelligenceService(config=config, store=ObservationStore(":memory:"))
    in_scope = RateChange(
        observation_key="ANZ|home_loan|special|12|unknown|fixed special",
        bank="ANZ",
        product_type=ProductType.HOME_LOAN,
        product_name="Fixed Special",
        rate_category="special",
        term_months=12,
        change_type=ChangeType.RATE_DECREASE,
        detected_at=DAY_ONE,
        previous_rate_pct=5.00,
        current_rate_pct=4.90,
        change_bps=-10.0,
    )
    out_of_scope = RateChange(
        observation_key="BNZ|home_loan|special|24|unknown|fixed special",
        bank="BNZ",
        product_type=ProductType.HOME_LOAN,
        product_name="Fixed Special",
        rate_category="special",
        term_months=24,
        change_type=ChangeType.RATE_DECREASE,
        detected_at=DAY_ONE,
        previous_rate_pct=5.00,
        current_rate_pct=4.50,
        change_bps=-50.0,
    )
    service.store.record_changes([in_scope, out_of_scope])

    changes = service.get_changes(days=30, now=DAY_ONE, scope_to_market=True)

    assert [change.bank for change in changes] == ["ANZ"]


def test_confirming_a_suspicious_movement_also_clears_its_companion_eligibility_event(
    make_record,
):
    """An over-threshold rate change bundled with an eligibility change
    produces two suspicious events sharing the same observation_key and
    detected_at (RATE_INCREASE/DECREASE + ELIGIBILITY_CHANGE). Reconfirming
    the same reading must clear both, not just the rate event a plain
    "most recent suspicious" lookup happens to pick (detect_changes() sorts
    rate events after their companions, so the rate row gets the higher id)."""
    confirmed = make_record(bank="ANZ", rate_pct=5.00, max_lvr=0.80, min_equity=0.20)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(
        bank="ANZ", rate_pct=8.00, max_lvr=0.75, min_equity=0.25, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [spike])]
    outcome_two = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
    assert {c.change_type for c in outcome_two.changes} == {
        ChangeType.RATE_INCREASE,
        ChangeType.ELIGIBILITY_CHANGE,
    }
    assert all(c.suspicious for c in outcome_two.changes), "sanity check: both flagged"

    repeat = make_record(
        bank="ANZ", rate_pct=8.00, max_lvr=0.75, min_equity=0.25, observed_at=DAY_THREE
    )
    service._collectors = [_StubCollector("ANZ", [repeat])]
    service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # clean re-observation

    changes = service.get_changes(days=30, now=DAY_THREE)
    assert len(changes) == 2
    assert all(not c.suspicious for c in changes), (
        "the companion eligibility event must be confirmed alongside the rate event"
    )


def test_a_multi_hop_reversion_leaves_its_stale_companion_event_quarantined(make_record):
    """confirmed 5.00% (max_lvr 0.80) -> warning 8.00% (max_lvr 0.75) ->
    warning 7.00% (max_lvr 0.70) -> clean 7.00% (max_lvr 0.70). The rate
    event rebases onto the last verified predecessor (5.00% -> 7.00%), but
    the companion ELIGIBILITY_CHANGE from the same run still describes the
    unverified intermediate hop (max_lvr 0.75 -> 0.70, B to C) rather than
    the accepted transition (A to C) -- clearing it too would present that
    stale diff as confirmed, so it is left quarantined instead."""
    day_four = DAY_ONE + timedelta(days=3)
    confirmed = make_record(bank="ANZ", rate_pct=5.00, max_lvr=0.80, min_equity=0.20)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(
        bank="ANZ", rate_pct=8.00, max_lvr=0.75, min_equity=0.25, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [spike])]
    service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # flagged suspicious

    partial_revert = make_record(
        bank="ANZ", rate_pct=7.00, max_lvr=0.70, min_equity=0.30, observed_at=DAY_THREE
    )
    service._collectors = [_StubCollector("ANZ", [partial_revert])]
    outcome_three = service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)
    assert {c.change_type for c in outcome_three.changes} == {
        ChangeType.RATE_DECREASE,
        ChangeType.ELIGIBILITY_CHANGE,
    }
    assert all(c.suspicious for c in outcome_three.changes), "sanity check: both flagged"

    repeat = make_record(
        bank="ANZ", rate_pct=7.00, max_lvr=0.70, min_equity=0.30, observed_at=day_four
    )
    service._collectors = [_StubCollector("ANZ", [repeat])]
    service.collect(captured_at_utc=day_four, persist_snapshot=False)  # clean re-observation

    changes = service.get_changes(days=30, now=day_four)
    confirmed_rate_events = [c for c in changes if not c.suspicious]
    eligibility_events = [c for c in changes if c.change_type is ChangeType.ELIGIBILITY_CHANGE]

    assert len(confirmed_rate_events) == 1
    assert confirmed_rate_events[0].previous_rate_pct == 5.00
    assert confirmed_rate_events[0].current_rate_pct == 7.00
    assert all(c.suspicious for c in eligibility_events), (
        "the companion still describes the unverified B -> C hop, not the accepted A -> C move"
    )


def test_a_confirmed_large_movement_counts_as_market_activity(fixture_fetcher):
    """A repeated 'suspicious' value is confirmed genuine, not permanently discounted."""

    def break_kiwibank(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>8.49%")
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=break_kiwibank)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # flagged suspicious
        service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # confirmed

        changes = service.get_changes(days=30, now=DAY_THREE)
        activity = service.get_market_activity(days=30, now=DAY_THREE)

    kiwibank_change = next(change for change in changes if change.bank == "Kiwibank")
    assert kiwibank_change.suspicious is False
    assert activity.changes >= 1


def test_a_spike_that_reverts_across_a_category_flip_is_not_confirmed(make_record):
    """Same bug as the sibling exact-reversion test, but the spike also
    flips rate_category (5.00% special -> warning 8.00% standard -> warning
    5.00% standard -> clean 5.00% standard). resolve_pairing() can't match
    the last confirmed special record to the standard warning chain at all
    -- rate_category is real identity in both observation_key() and
    eligibility_group_key() -- so it must fall back to the category-agnostic
    match, the same way _suspicious_predecessor_keys() already does."""
    day_four = DAY_ONE + timedelta(days=3)
    confirmed = make_record(bank="ANZ", rate_category="special", rate_pct=5.00)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(
        bank="ANZ", rate_category="standard", rate_pct=8.00, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [spike])]
    service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # flagged suspicious

    revert = make_record(
        bank="ANZ", rate_category="standard", rate_pct=5.00, observed_at=DAY_THREE
    )
    service._collectors = [_StubCollector("ANZ", [revert])]
    service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # also flagged suspicious

    repeat = make_record(
        bank="ANZ", rate_category="standard", rate_pct=5.00, observed_at=day_four
    )
    service._collectors = [_StubCollector("ANZ", [repeat])]
    service.collect(captured_at_utc=day_four, persist_snapshot=False)  # clean re-observation

    changes = service.get_changes(days=30, now=day_four)
    activity = service.get_market_activity(days=30, now=day_four)

    reversal = next(c for c in changes if c.previous_rate_pct == 8.00 and c.current_rate_pct == 5.00)
    assert reversal.suspicious is True, "the bogus reversal must stay excluded from market activity"
    assert activity.changes == 0, "the verified rate never actually changed"
    # The category never reverted (it stays "standard" throughout), so the
    # confirmed "special" predecessor is never matched by anything in any
    # run's accepted records -- and by day_four the record reporting it
    # isn't itself in suspicious_keys either, so only _suspicious_predecessor_keys()
    # alone would no longer protect it.
    assert ChangeType.REMOVED_PRODUCT not in {c.change_type for c in changes}, (
        "the verified special line must not be withdrawn just because this run's "
        "record isn't itself flagged suspicious"
    )
    snapshot = service.get_market_snapshot(now=day_four)
    assert snapshot.term(12).rate_for("ANZ") == 5.00


def test_a_spike_that_reverts_is_not_confirmed_as_a_genuine_movement(make_record):
    """confirmed 5.00% -> warning 8.00% -> warning 5.00% -> clean 5.00% must
    not confirm the latest suspicious event (8.00% -> 5.00%) as a genuine
    -300 bps move: 8.00% was itself never confirmed, and the market view
    never actually left 5.00%."""
    day_four = DAY_ONE + timedelta(days=3)
    confirmed = make_record(bank="ANZ", rate_pct=5.00)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(bank="ANZ", rate_pct=8.00, observed_at=DAY_TWO)
    service._collectors = [_StubCollector("ANZ", [spike])]
    service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # flagged suspicious

    revert = make_record(bank="ANZ", rate_pct=5.00, observed_at=DAY_THREE)
    service._collectors = [_StubCollector("ANZ", [revert])]
    service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # also flagged suspicious

    repeat = make_record(bank="ANZ", rate_pct=5.00, observed_at=day_four)
    service._collectors = [_StubCollector("ANZ", [repeat])]
    service.collect(captured_at_utc=day_four, persist_snapshot=False)  # clean re-observation

    changes = service.get_changes(days=30, now=day_four)
    activity = service.get_market_activity(days=30, now=day_four)

    reversal = next(c for c in changes if c.previous_rate_pct == 8.00 and c.current_rate_pct == 5.00)
    assert reversal.suspicious is True, "the bogus reversal must stay excluded from market activity"
    assert activity.changes == 0, "the market view never left the original confirmed 5.00%"


def test_a_partial_reversion_is_confirmed_relative_to_the_last_verified_rate(make_record):
    """confirmed 5.00% -> warning 8.00% -> warning 7.00% -> clean 7.00% must
    confirm the movement relative to the last verified rate (5.00% -> 7.00%,
    +200 bps), not the stale intermediate hop (8.00% -> 7.00%, -100 bps)."""
    day_four = DAY_ONE + timedelta(days=3)
    confirmed = make_record(bank="ANZ", rate_pct=5.00)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(bank="ANZ", rate_pct=8.00, observed_at=DAY_TWO)
    service._collectors = [_StubCollector("ANZ", [spike])]
    service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # flagged suspicious

    partial_revert = make_record(bank="ANZ", rate_pct=7.00, observed_at=DAY_THREE)
    service._collectors = [_StubCollector("ANZ", [partial_revert])]
    service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # also flagged suspicious

    repeat = make_record(bank="ANZ", rate_pct=7.00, observed_at=day_four)
    service._collectors = [_StubCollector("ANZ", [repeat])]
    service.collect(captured_at_utc=day_four, persist_snapshot=False)  # clean re-observation

    changes = service.get_changes(days=30, now=day_four)
    activity = service.get_market_activity(days=30, now=day_four)

    confirmed_change = next(c for c in changes if not c.suspicious)
    assert confirmed_change.previous_rate_pct == 5.00
    assert confirmed_change.current_rate_pct == 7.00
    assert confirmed_change.change_bps == 200.0
    assert confirmed_change.change_type is ChangeType.RATE_INCREASE
    assert activity.changes == 1
    assert activity.mean_change_bps == 200.0


def test_a_spike_that_reverts_across_an_eligibility_key_change_is_not_confirmed(make_record):
    """Same bug as the sibling test above, but the spike also changes
    eligibility identity (key A -> key B): the last confirmed state is filed
    under A while the whole warning chain lives under B, so an exact-key
    lookup for "was this reconfirmed?" always misses it and treats the
    missing key as proof of a genuine move."""
    day_four = DAY_ONE + timedelta(days=3)
    confirmed = make_record(bank="ANZ", rate_pct=5.00, max_lvr=0.80, min_equity=0.20)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(
        bank="ANZ", rate_pct=8.00, max_lvr=0.75, min_equity=0.25, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [spike])]
    service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # flagged suspicious

    revert = make_record(
        bank="ANZ", rate_pct=5.00, max_lvr=0.75, min_equity=0.25, observed_at=DAY_THREE
    )
    service._collectors = [_StubCollector("ANZ", [revert])]
    service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # also flagged suspicious

    repeat = make_record(
        bank="ANZ", rate_pct=5.00, max_lvr=0.75, min_equity=0.25, observed_at=day_four
    )
    service._collectors = [_StubCollector("ANZ", [repeat])]
    service.collect(captured_at_utc=day_four, persist_snapshot=False)  # clean re-observation

    changes = service.get_changes(days=30, now=day_four)
    activity = service.get_market_activity(days=30, now=day_four)

    reversal = next(c for c in changes if c.previous_rate_pct == 8.00 and c.current_rate_pct == 5.00)
    assert reversal.suspicious is True, "the bogus reversal must stay excluded from market activity"
    assert activity.changes == 0, "the market view never left the original confirmed 5.00%"


def test_a_suspicious_observation_falls_back_to_its_last_confirmed_rate(fixture_fetcher):
    """An unconfirmed parser artefact must not feed market-derived pricing --
    but the bank/term must fall back to its last confirmed rate, not vanish."""

    def break_kiwibank(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>8.49%")
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=break_kiwibank)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        snapshot = service.get_market_snapshot(now=DAY_TWO)

    assert snapshot.term(36).rate_for("Kiwibank") == 5.35


def test_a_suspicious_observation_with_an_eligibility_edit_still_falls_back(service, make_record):
    """The eligibility-group fallback must also resolve a suspicious tag's predecessor.

    An eligibility edit changes observation_key(), so an exact-key lookup
    into the confirmed set would miss the very predecessor a suspicious
    record needs to fall back to -- dropping it from the view exactly as if
    no fallback existed at all.
    """
    confirmed = make_record(bank="ANZ", max_lvr=0.80, min_equity=0.20, rate_pct=4.79)
    suspicious = make_record(
        bank="ANZ",
        max_lvr=0.75,
        min_equity=0.25,
        rate_pct=1.50,
        validation_status="warning",
        observed_at=DAY_TWO,
    )
    service.store.record_observations([confirmed], observed_at=DAY_ONE)
    service.store.record_observations([suspicious], observed_at=DAY_TWO)

    snapshot = service.get_market_snapshot(now=DAY_TWO)

    assert snapshot.term(12).rate_for("ANZ") == 4.79


def test_a_suspicious_observation_with_a_rate_category_flip_still_falls_back(make_record):
    """A rate_category flip changes both observation_key() and
    eligibility_group_key(), so resolve_pairing() can pair neither the
    suspicious record itself nor -- critically -- its predecessor during
    withdrawal detection. Without collect() suppressing that withdrawal,
    detect_changes() reports the predecessor REMOVED_PRODUCT, which
    permanently excludes it from get_last_confirmed_observations() -- the
    very fallback source the category-agnostic substitution in
    _resolve_suspicious() depends on. Driven through the real collect()
    pipeline (via a stub collector, so product_name can be held constant --
    see _StubCollector) rather than reimplementing its steps, so this fails
    if collect()'s own wiring regresses, not just the underlying helpers.
    """
    confirmed = make_record(bank="ANZ", rate_category="special", rate_pct=4.79)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    suspicious = make_record(
        bank="ANZ", rate_category="standard", rate_pct=1.50, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [suspicious])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    snapshot = service.get_market_snapshot(now=DAY_TWO)

    assert outcome.validation.suspicious_keys, "the movement must be flagged suspicious"
    assert {c.change_type for c in outcome.changes} == {ChangeType.NEW_PRODUCT}, (
        "the predecessor's withdrawal must be suppressed, not merely its effect papered over"
    )
    assert all(c.suspicious for c in outcome.changes), (
        "the new-product event itself must carry the suspicious flag through to "
        "market activity and changes --alerts, not just quarantine the snapshot"
    )
    assert snapshot.term(12).rate_for("ANZ") == 4.79


def test_a_clean_category_flip_with_no_rate_movement_still_withdraws_its_predecessor(
    make_record,
):
    """A category relabelling with no out-of-threshold rate movement (e.g.
    "4.79% special" -> "4.79% standard") is never flagged suspicious in the
    first place -- validate_observations() only compares movement against a
    resolve_pairing() match, and a category flip has none to compare
    against. _last_confirmed_predecessors() still category-agnostically
    matches the old "special" line as this run's best-effort predecessor
    (it is used for confirmation rebasing too), but that must not by itself
    suppress the old key's withdrawal: this run's record was never
    quarantined, so there is no confirmation to protect. Suppressing it
    anyway would leave a stale "special" observation feeding the special-rate
    market snapshot forever, or launder a category-only parser fault as a
    verified market event -- exactly what AGENTS.md's "a parser fault cannot
    fake a repricing round" invariant rules out.
    """
    # Two tracked products: term 12 flips category below, term 24 stays
    # exactly matched -- giving ANZ 1/2 known-key coverage, enough to stay
    # in scope for withdrawal detection under the default 0.5 ratio (a
    # single-product bank would otherwise have its category flip look
    # identical to a partial-parse regression to _reliably_collected_banks,
    # a different, unrelated suppression mechanism).
    baseline = [
        make_record(bank="ANZ", term_months=12, rate_category="special", rate_pct=4.79),
        make_record(bank="ANZ", term_months=24, rate_category="special", rate_pct=4.99),
    ]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", baseline)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    relabelled = make_record(
        bank="ANZ", term_months=12, rate_category="standard", rate_pct=4.79, observed_at=DAY_TWO
    )
    unchanged = make_record(
        bank="ANZ", term_months=24, rate_category="special", rate_pct=4.99, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [relabelled, unchanged])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert not outcome.validation.suspicious_keys, (
        "sanity check: a category flip with no rate movement is never flagged suspicious"
    )
    assert ChangeType.REMOVED_PRODUCT in {c.change_type for c in outcome.changes}, (
        "the old special-category line was never quarantined, so its withdrawal must not "
        "be suppressed by the confirmation-protection path"
    )


def test_a_suspicious_category_flip_that_stays_suspicious_still_protects_the_confirmed_key(
    make_record,
):
    """A category flip that remains suspicious for a second run (5.00%
    special -> 8.00% standard warning -> 7.00% standard warning) gives the
    second run's record an exact warning-tagged predecessor (itself, from
    the first flip), which must not stop the category-agnostic fallback
    from also protecting the confirmed special key from before the flip --
    that's a different, still-orphaned baseline record."""
    confirmed = make_record(bank="ANZ", rate_category="special", rate_pct=5.00)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    first_flip = make_record(
        bank="ANZ", rate_category="standard", rate_pct=8.00, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [first_flip])]
    outcome_two = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
    assert outcome_two.validation.suspicious_keys, "sanity check: run 2 flagged suspicious"

    still_suspicious = make_record(
        bank="ANZ", rate_category="standard", rate_pct=7.00, observed_at=DAY_THREE
    )
    service._collectors = [_StubCollector("ANZ", [still_suspicious])]
    outcome_three = service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)

    snapshot = service.get_market_snapshot(now=DAY_THREE)

    assert outcome_three.validation.suspicious_keys, "sanity check: run 3 still flagged suspicious"
    assert ChangeType.REMOVED_PRODUCT not in {c.change_type for c in outcome_three.changes}, (
        "the confirmed special key must stay protected, not reported withdrawn"
    )
    assert snapshot.term(12).rate_for("ANZ") == 5.00


def test_a_partial_correction_confirms_as_one_event_not_two(make_record):
    """A third reading that differs from the warning value it confirms
    (5.00% confirmed -> 6.00% warning -> 5.50% clean, the 6.00% -> 5.50%
    move itself not suspicious since it's within threshold of the warning
    baseline) must produce exactly one change event describing the true
    5.00% -> 5.50% movement -- not the stale 5.00% -> 6.00% event confirmed
    as-is plus a separate clean 6.00% -> 5.50% event, which would
    double-count one real reprice as two and skew market-activity
    statistics (mean/count of movements)."""
    confirmed = make_record(bank="ANZ", rate_pct=5.00)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    spike = make_record(bank="ANZ", rate_pct=6.00, observed_at=DAY_TWO)
    service._collectors = [_StubCollector("ANZ", [spike])]
    outcome_two = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
    assert outcome_two.validation.suspicious_keys, "sanity check: run 2 flagged suspicious"

    correction = make_record(bank="ANZ", rate_pct=5.50, observed_at=DAY_THREE)
    service._collectors = [_StubCollector("ANZ", [correction])]
    outcome_three = service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)
    assert not outcome_three.validation.suspicious_keys, (
        "sanity check: run 3's move from the warning baseline is within threshold"
    )

    changes = service.get_changes(days=30, now=DAY_THREE)
    rate_moves = [c for c in changes if c.is_rate_move]

    assert len(rate_moves) == 1
    assert rate_moves[0].previous_rate_pct == 5.00
    assert rate_moves[0].current_rate_pct == 5.50
    assert rate_moves[0].change_bps == 50.0
    assert rate_moves[0].suspicious is False


def test_a_rate_category_flip_still_falls_back_when_product_name_changes_too(make_record):
    """The generic extractor derives product_name by searching the row for the
    category text (extractors._product_name), so a real category flip usually
    changes product_name right along with it (e.g. "Fixed Standard" -> "Fixed
    Special") -- unlike the sibling test above, which (deliberately, per its
    own docstring) holds product_name constant. category_agnostic_group_key()
    must not include product_name, or this realistic case defeats the very
    fallback it exists for.
    """
    confirmed = make_record(
        bank="ANZ", rate_category="special", product_name="Fixed Special", rate_pct=4.79
    )
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    suspicious = make_record(
        bank="ANZ",
        rate_category="standard",
        product_name="Fixed Standard",
        rate_pct=1.50,
        observed_at=DAY_TWO,
    )
    service._collectors = [_StubCollector("ANZ", [suspicious])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    snapshot = service.get_market_snapshot(now=DAY_TWO)

    assert outcome.validation.suspicious_keys, "the movement must be flagged suspicious"
    assert snapshot.term(12).rate_for("ANZ") == 4.79


def test_a_rejected_record_with_a_rate_category_flip_does_not_withdraw_its_predecessor(
    make_record,
):
    """Mirrors the suspicious-record case, but for a hard-rejected line.

    A rejected record whose rate_category also flipped can't be paired by
    resolve_pairing() either (rate_category is real identity there too), so
    without the category-agnostic fallback in _rejected_predecessor_keys()
    its predecessor is reported REMOVED_PRODUCT purely because a parser
    fault renamed its category -- even though ANZ's other term still passed
    validation, keeping ANZ in scope for withdrawal detection at all.
    """
    baseline = [
        make_record(bank="ANZ", term_months=12, rate_category="special", rate_pct=4.79),
        make_record(bank="ANZ", term_months=24, rate_category="special", rate_pct=4.99),
    ]
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", baseline)],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    # term 12 flips category and becomes implausible (rejected); term 24 is
    # unchanged and accepted, giving ANZ 1/2 known-key coverage -- enough to
    # stay in scope for withdrawal detection under the default 0.5 ratio.
    rejected_line = make_record(
        bank="ANZ", term_months=12, rate_category="standard", rate_pct=99.0, observed_at=DAY_TWO
    )
    unchanged_line = make_record(
        bank="ANZ", term_months=24, rate_category="special", rate_pct=4.99, observed_at=DAY_TWO
    )
    service._collectors = [_StubCollector("ANZ", [rejected_line, unchanged_line])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    assert outcome.validation.rejected, "sanity check: the line really was rejected"
    withdrawals = [
        change for change in outcome.changes if change.change_type is ChangeType.REMOVED_PRODUCT
    ]
    assert withdrawals == []
    snapshot = service.get_market_snapshot(now=DAY_TWO)
    assert snapshot.term(12).rate_for("ANZ") == 4.79


def test_resolve_suspicious_does_not_double_count_an_independently_present_fallback(make_record):
    """Unlike an eligibility edit (always superseded out of the current view),
    a rate-category flip's predecessor is a different eligibility group and
    is never superseded -- it can be genuinely, independently present in
    ``records`` in its own right. The fallback substitution must not add a
    second copy of it.
    """
    confirmed = make_record(bank="ANZ", rate_category="special", rate_pct=4.79)
    suspicious = make_record(
        bank="ANZ", rate_category="standard", rate_pct=1.50, validation_status="warning"
    )

    resolved = _resolve_suspicious([confirmed, suspicious], confirmed=[confirmed])

    assert len(resolved) == 1
    assert resolved[0].advertised_rate_pct == 4.79


def test_a_suspicious_reversion_falls_back_to_the_immediate_predecessor(service, make_record):
    """Reverting to a stale earlier key must fall back to the latest confirmed
    state (the one it actually superseded), not the stale reverted-to key."""
    # Key A confirmed.
    service.store.record_observations(
        [make_record(bank="ANZ", max_lvr=0.80, min_equity=0.20, rate_pct=4.79)],
        observed_at=DAY_ONE,
    )
    # Key B, an eligibility transition, supersedes A -- also confirmed.
    service.store.record_observations(
        [make_record(bank="ANZ", max_lvr=0.75, min_equity=0.25, rate_pct=4.99)],
        observed_at=DAY_TWO,
    )
    # A suspicious observation reverts to A's condition with an implausible rate.
    service.store.record_observations(
        [
            make_record(
                bank="ANZ",
                max_lvr=0.80,
                min_equity=0.20,
                rate_pct=1.50,
                validation_status="warning",
            )
        ],
        observed_at=DAY_THREE,
    )

    snapshot = service.get_market_snapshot(now=DAY_THREE)

    assert snapshot.term(12).rate_for("ANZ") == 4.99


def test_a_non_comparable_product_is_excluded_from_the_market_snapshot(service, make_record):
    """The default excluded_product_names config must reach the live snapshot,
    not just build_market_snapshot() in isolation -- see ASBCollector's
    "Back My Build" docstring."""
    service.store.record_observations(
        [
            make_record(bank="ASB", rate_pct=3.99, product_name="Back My Build"),
            make_record(bank="ASB", rate_pct=4.89, product_name="Fixed Special"),
            make_record(bank="BNZ", rate_pct=4.85),
        ],
        observed_at=DAY_ONE,
    )

    snapshot = service.get_market_snapshot(now=DAY_ONE)

    assert snapshot.term(12).rate_for("ASB") == 4.89


def test_a_suspicious_observation_is_excluded_from_the_competitor_assumption(fixture_fetcher):
    def break_kiwibank(bank, html):
        if bank == "Kiwibank":
            return html.replace("<td>3 years</td><td>5.35%", "<td>3 years</td><td>8.49%")
        return html

    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=break_kiwibank)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        assumption = service.resolve_competitor_assumption(
            "MARKET_LEADER", term_months=36, now=DAY_TWO
        )

    assert assumption.rate_pct != 8.49


def test_a_stale_observation_is_excluded_from_the_market_snapshot(fixture_fetcher):
    """A bank silent well past the freshness window must drop out of live pricing."""
    with build_service(fixture_fetcher(), max_observation_age_hours=24) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        stale_snapshot = service.get_market_snapshot(now=DAY_ONE + timedelta(days=10))
        fresh_snapshot = service.get_market_snapshot(now=DAY_ONE + timedelta(hours=1))

    assert stale_snapshot.term(12) is None
    assert fresh_snapshot.term(12) is not None


@pytest.mark.parametrize(
    ("product_type", "supplied", "expected"),
    [
        (ProductType.HOME_LOAN, None, "special"),
        (ProductType.TERM_DEPOSIT, None, "term_deposit"),
        (ProductType.HOME_LOAN, "standard", "standard"),
        (ProductType.TERM_DEPOSIT, "special", "special"),
    ],
)
def test_resolve_rate_category_defaults_per_product_but_honours_an_explicit_value(
    product_type, supplied, expected
):
    assert _resolve_rate_category(product_type, supplied) == expected


def _deposit_service(make_record, *records_kwargs):
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
    )
    service.store.record_observations(
        [
            make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit", **kw)
            for kw in records_kwargs
        ]
    )
    return service


def test_resolve_competitor_assumption_defaults_to_the_deposit_category(make_record):
    """product_type=TERM_DEPOSIT alone, with no explicit rate_category, must
    resolve against the deposit market's own default category rather than
    the mortgage-specific "special" literal, which matches no deposit
    records and previously made this raise "requires advertised rates"."""
    service = _deposit_service(
        make_record,
        {"bank": "ANZ", "rate_pct": 4.00},
        {"bank": "BNZ", "rate_pct": 4.20},
    )

    assumption = service.resolve_competitor_assumption(
        strategy=CompetitorRateStrategy.MARKET_MEDIAN, product_type=ProductType.TERM_DEPOSIT
    )

    assert assumption.rate_pct == pytest.approx(4.10)


def test_get_competitive_position_defaults_to_the_deposit_category(make_record):
    """Same bug via get_competitive_position(): previously raised
    ValueError("no advertised market data...") for a deposit position with
    no explicit rate_category."""
    service = _deposit_service(make_record, {"bank": "ANZ", "rate_pct": 4.00})

    position = service.get_competitive_position(
        our_rate=4.10, product_type=ProductType.TERM_DEPOSIT
    )

    assert position.market_best == 4.00


def test_resolve_competitor_assumption_does_not_blend_categories(service, make_record):
    """rate_category=None (this method's own default) must resolve to the
    product's comparable category before reaching get_market_snapshot(),
    not forward None straight through -- that method's own None now means
    the unfiltered view across every category (see _RATE_CATEGORY_OMITTED),
    which would let MARKET_MEDIAN silently blend special and standard
    mortgage tiers into one competitor assumption fed straight into the
    optimiser."""
    service.store.record_observations(
        [
            make_record(bank="ANZ", rate_category="special", rate_pct=4.00),
            make_record(bank="BNZ", rate_category="standard", rate_pct=6.00),
        ],
        observed_at=DAY_ONE,
    )

    assumption = service.resolve_competitor_assumption(strategy=CompetitorRateStrategy.MARKET_MEDIAN)

    assert assumption.rate_pct == pytest.approx(4.00)


def test_get_competitive_position_does_not_blend_categories(service, make_record):
    """Same fix as resolve_competitor_assumption(), for get_competitive_position().
    Asserted via market_worst (6.00 from BNZ's standard rate would leak in
    unfiltered), not market_best: the lower "special" rate would win either
    way and so would not, by itself, expose the blending bug."""
    service.store.record_observations(
        [
            make_record(bank="ANZ", rate_category="special", rate_pct=4.00),
            make_record(bank="BNZ", rate_category="standard", rate_pct=6.00),
        ],
        observed_at=DAY_ONE,
    )

    position = service.get_competitive_position(our_rate=4.10)

    assert position.market_best == 4.00
    assert position.market_worst == 4.00


def test_the_market_snapshot_covers_every_bank(service):
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
    snapshot = service.get_market_snapshot(now=DAY_ONE)

    assert set(snapshot.banks) == {"ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"}
    market = snapshot[12]
    assert market.best_rate == 4.79
    assert market.market_leader == "ANZ"
    assert market.bank_count == 5


def test_market_snapshot_rate_category_none_is_unfiltered(service, make_record):
    """rate_category=None, passed explicitly, must give the unfiltered view
    across every category -- matching build_market_snapshot()'s own
    contract -- not the smart per-product default _resolve_rate_category()
    applies when the argument is omitted entirely."""
    service.store.record_observations(
        [
            make_record(bank="ANZ", rate_category="special", rate_pct=4.79),
            make_record(bank="BNZ", rate_category="standard", rate_pct=5.39),
        ],
        observed_at=DAY_ONE,
    )

    default_snapshot = service.get_market_snapshot(now=DAY_ONE)
    unfiltered_snapshot = service.get_market_snapshot(rate_category=None, now=DAY_ONE)

    assert set(default_snapshot.banks) == {"ANZ"}
    assert set(unfiltered_snapshot.banks) == {"ANZ", "BNZ"}


def test_previous_market_snapshot_rate_category_none_is_unfiltered(service, make_record):
    """Same fix as get_market_snapshot(), for the previous-run snapshot."""
    service.store.record_observations(
        [
            make_record(bank="ANZ", rate_category="special", rate_pct=4.79),
            make_record(bank="BNZ", rate_category="standard", rate_pct=5.39),
        ],
        observed_at=DAY_ONE,
    )
    service.store.record_observations(
        [make_record(bank="ANZ", rate_category="special", rate_pct=4.89)], observed_at=DAY_TWO
    )

    default_snapshot = service.get_previous_market_snapshot()
    unfiltered_snapshot = service.get_previous_market_snapshot(rate_category=None)

    assert set(default_snapshot.banks) == {"ANZ"}
    assert set(unfiltered_snapshot.banks) == {"ANZ", "BNZ"}


def test_market_snapshot_reconstructs_an_older_run_by_id(fixture_fetcher):
    """last_seen_run_id gets overwritten by later unchanged runs; the snapshot must not."""
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        run_two = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)  # unchanged
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        service.collect(captured_at_utc=DAY_THREE, persist_snapshot=False)  # ANZ reprices

        snapshot_at_run_two = service.get_market_snapshot(
            as_of_run_id=run_two.write_result.run_id
        )

    assert snapshot_at_run_two.term(12).rate_for("ANZ") == 4.79


def test_market_snapshot_as_of_run_id_resolves_a_same_timestamp_run(service, make_record):
    """Two runs sharing observed_at, with the rate changing between them, must
    let as_of_run_id reconstruct the specific run named -- not whichever
    same-timestamp run a plain timestamp comparison happens to prefer."""
    earlier = service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.60)], observed_at=DAY_TWO
    )
    service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.70)], observed_at=DAY_TWO
    )

    snapshot = service.get_market_snapshot(as_of_run_id=earlier.run_id)

    assert snapshot.term(12).rate_for("ANZ") == 4.60


def test_market_snapshot_as_of_a_suspicious_run_falls_back_to_the_confirmed_rate(
    service, make_record
):
    """A historical reconstruction of a suspicious run must not drop the bank."""
    service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], observed_at=DAY_ONE
    )
    write_result = service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=1.50, validation_status="warning")],
        observed_at=DAY_TWO,
    )

    snapshot = service.get_market_snapshot(as_of_run_id=write_result.run_id)

    assert snapshot.term(12).rate_for("ANZ") == 4.79


def test_previous_market_snapshot_falls_back_to_the_confirmed_rate(service, make_record):
    """The previous-run snapshot must not drop a bank whose latest state was suspicious."""
    service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], observed_at=DAY_ONE
    )
    service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=1.50, validation_status="warning")],
        observed_at=DAY_TWO,
    )
    service.store.record_observations(
        [make_record(bank="BNZ", rate_pct=4.85)], observed_at=DAY_THREE
    )

    snapshot = service.get_previous_market_snapshot()

    assert snapshot.term(12).rate_for("ANZ") == 4.79


def test_previous_market_snapshot_is_scoped_to_its_own_product(service, make_record):
    """Home-loan and term-deposit collections now share one collection_runs
    table (the scheduled workflow can run both). A deposit run D1 followed
    by an unrelated mortgage run M1 must not make
    get_previous_market_snapshot(product_type=TERM_DEPOSIT) reconstruct
    "as of D1" -- D1 is the only deposit run so far, so there is no previous
    deposit snapshot yet, and the mortgage run in between must not be
    mistaken for one."""
    service.store.record_observations(
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        observed_at=DAY_ONE,
    )
    service.store.record_observations(
        [make_record(product_type=ProductType.HOME_LOAN)], observed_at=DAY_TWO
    )

    assert service.get_previous_market_snapshot(product_type=ProductType.TERM_DEPOSIT) is None


def test_history_is_queryable_after_a_reprice(fixture_fetcher):
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        history = service.get_rate_history(bank="ANZ", term_months=12, rate_category="special")

    assert [record.advertised_rate_pct for record in history] == [4.79, 4.59]


def test_get_rate_history_excludes_states_first_observed_after_now(fixture_fetcher):
    """A historical now= must not expose a state first observed after that
    point, matching get_changes()'s own upper bound."""
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

        as_of_day_one = service.get_rate_history(
            bank="ANZ", term_months=12, rate_category="special", now=DAY_ONE
        )

    assert [record.advertised_rate_pct for record in as_of_day_one] == [4.79]


def test_rate_history_series_uses_the_most_competitive_simultaneous_product(make_record):
    """Mirrors build_market_snapshot()'s own representative-rate selection:
    when a bank advertises several simultaneous special products/LVR tiers at
    one term, the chart series must use the same most-competitive one -- not
    whichever row a naive same-date collapse happens to keep."""
    tier_a = make_record(bank="ANZ", product_name="LVR Tier A", rate_pct=4.79)
    tier_b = make_record(bank="ANZ", product_name="LVR Tier B", rate_pct=5.19)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [tier_a, tier_b])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    series = service.get_rate_history_series(term_months=12, rate_category="special")

    assert series[-1][1]["ANZ"] == 4.79


def test_rate_history_series_keeps_a_surviving_product_when_a_sibling_is_withdrawn(make_record):
    """A same-category, same-term product withdrawal must not blank a
    surviving sibling product's representative rate."""
    tier_a = make_record(bank="ANZ", product_name="LVR Tier A", rate_pct=4.79)
    tier_b = make_record(bank="ANZ", product_name="LVR Tier B", rate_pct=5.19)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [tier_a, tier_b])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    service._collectors = [_StubCollector("ANZ", [tier_b])]
    outcome = service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
    assert {c.change_type for c in outcome.changes} == {ChangeType.REMOVED_PRODUCT}, (
        "sanity check: Tier A really was detected withdrawn"
    )

    series = service.get_rate_history_series(term_months=12, rate_category="special")

    assert series[-1][1]["ANZ"] == 5.19


def test_rate_history_series_falls_back_to_the_confirmed_rate_when_suspicious(
    service, make_record
):
    """A warning-tagged (suspicious) state must not flow straight into the
    chart -- matches get_market_snapshot()'s own confirmed-rate fallback, so
    a parser fault doesn't visibly move the history the live view correctly
    ignores."""
    service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], observed_at=DAY_ONE
    )
    service.store.record_observations(
        [make_record(bank="ANZ", rate_pct=1.50, validation_status="warning")],
        observed_at=DAY_TWO,
    )

    series = service.get_rate_history_series(term_months=12, rate_category="special")

    assert series[-1][1]["ANZ"] == 4.79


def test_rate_history_series_is_scoped_to_configured_banks(make_record):
    """The live snapshot applies selected_banks; the history series must too,
    or the chart can show a bank the current grid correctly hides."""
    config = PricingIntelligenceConfig(
        database_path=":memory:",
        selected_banks=["ANZ"],
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    service = PricingIntelligenceService(config=config, store=ObservationStore(":memory:"))
    service.store.record_observations(
        [
            make_record(bank="ANZ", rate_pct=4.79),
            make_record(bank="BNZ", rate_pct=4.85),
        ],
        observed_at=DAY_ONE,
    )

    series = service.get_rate_history_series(term_months=12, rate_category="special")

    assert "BNZ" not in series[-1][1]


def test_changes_are_queryable_by_window(fixture_fetcher):
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

        recent = service.get_changes(days=7, now=DAY_TWO)
        assert len(recent) == 1
        assert service.get_changes(days=7, bank="BNZ", now=DAY_TWO) == []


def test_competitive_position_is_computed_against_the_live_market(service):
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
    position = service.get_competitive_position(our_rate=4.95, term_months=12, now=DAY_ONE)

    assert position.market_best == 4.79
    assert position.our_rate_minus_best_bps == 16.0
    assert position.competitors_below_us == 5


def test_competitive_position_reports_a_missing_term_clearly(service):
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    with pytest.raises(ValueError, match="no advertised market data at 120 months"):
        service.get_competitive_position(our_rate=4.95, term_months=120, now=DAY_ONE)


def test_the_service_resolves_a_competitor_assumption(service):
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    median = service.resolve_competitor_assumption("MARKET_MEDIAN", term_months=12, now=DAY_ONE)
    leader = service.resolve_competitor_assumption("MARKET_LEADER", term_months=12, now=DAY_ONE)

    assert median.rate_pct == 4.85
    assert leader.rate_pct == 4.79
    assert leader.competitor_loan_rate == 0.0479


def test_a_write_failure_leaves_no_partial_observation_state(make_record, monkeypatch):
    """If record_changes() (or confirmation) raises after observations would
    otherwise have already committed, the whole collection must roll back --
    not leave a new observation state with no corresponding change event,
    which a retry could never reconstruct (it would compare against the
    already-updated state and see no movement)."""
    confirmed = make_record(bank="ANZ", rate_pct=5.00)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            database_path=":memory:", collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[_StubCollector("ANZ", [confirmed])],
    )
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)  # baseline, bootstrapped

    reprice = make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)
    service._collectors = [_StubCollector("ANZ", [reprice])]

    def failing_record_changes(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(service.store, "record_changes", failing_record_changes)

    with pytest.raises(RuntimeError, match="simulated failure"):
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)

    # Must still reflect DAY_ONE's baseline, not DAY_TWO's reprice -- the
    # failed write must not have partially committed.
    current = service.store.get_current_observations()
    assert [record.advertised_rate_pct for record in current] == [5.00]


def test_a_dry_run_stores_nothing(service):
    outcome = service.collect(captured_at_utc=DAY_ONE, persist=False, persist_snapshot=False)

    assert outcome.write_result is None
    assert service.store.get_latest_rates() == []


def test_a_dry_run_previews_changes_without_persisting_them(fixture_fetcher):
    """--no-persist must still show what a real collection would detect."""
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        outcome = service.collect(
            captured_at_utc=DAY_TWO, persist=False, persist_snapshot=False
        )
        stored = service.store.get_latest_rates(bank="ANZ", term_months=12)[0]
        persisted_changes = service.store.get_changes_since(days=7, now=DAY_TWO)

    assert outcome.write_result is None
    assert len(outcome.changes) == 1
    assert outcome.changes[0].change_type is ChangeType.RATE_DECREASE
    assert stored.advertised_rate_pct == 4.79, "the dry run must not persist the reprice"
    assert persisted_changes == [], "nor its change event"


def test_a_run_where_every_bank_fails_is_reported_as_such(fixture_fetcher):
    banks = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]
    with build_service(fixture_fetcher(failing_banks=banks)) as service:
        outcome = service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

    assert not outcome.report.any_success
    assert outcome.rate_count == 0
    assert outcome.write_result is None

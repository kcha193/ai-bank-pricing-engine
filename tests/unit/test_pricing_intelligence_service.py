"""End-to-end service behaviour, driven entirely by saved fixtures."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.changes import ChangeType, detect_changes
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.market_state import MarketState
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)
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


def test_the_market_snapshot_covers_every_bank(service):
    service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
    snapshot = service.get_market_snapshot(now=DAY_ONE)

    assert set(snapshot.banks) == {"ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"}
    market = snapshot[12]
    assert market.best_rate == 4.79
    assert market.market_leader == "ANZ"
    assert market.bank_count == 5


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


def test_history_is_queryable_after_a_reprice(fixture_fetcher):
    with build_service(fixture_fetcher()) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=cut_anz_one_year)
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
        history = service.get_rate_history(bank="ANZ", term_months=12, rate_category="special")

    assert [record.advertised_rate_pct for record in history] == [4.79, 4.59]


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

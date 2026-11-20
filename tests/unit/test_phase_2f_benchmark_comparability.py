"""Phase 2f: deposit reference tiers, inferred mortgage eligibility, own-bank identity.

Findings DATA-005 (a $500k+ tier or an incompatible payment frequency could set the default
term-deposit benchmark), DATA-013 (an 80% LVR inferred from a special-rate label was
indistinguishable from a published one) and DATA-014 (``our_bank="ASB Bank"`` did not exclude
ASB). Test ids T01-T39 refer to the Phase 2f brief; T40-T42 are the existing suites.

Fixtures are synthetic or the trimmed 2026-10-30 captures. No live bank calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from balance_pricing_engine.competitor_rates.bank_identity import (
    UnknownBankError,
    canonical_bank,
    same_bank,
)
from balance_pricing_engine.competitor_rates.collectors import (
    COLLECTORS_BY_BANK,
    build_collectors,
)
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    CompetitorRatesConfig,
    ProductType,
)
from balance_pricing_engine.config import load_config
from balance_pricing_engine.config_identity import effective_config_hash
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.coverage import assess_benchmark_coverage
from balance_pricing_engine.pricing_intelligence.deposit_reference import (
    DepositReferencePolicy,
)
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import (
    resolve_competitor_rate,
    resolve_market_overlay,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pricing_intelligence"
LIVE_TD = FIXTURES / "term_deposits_2026-10-30"
BANKS = ("ANZ", "ASB", "BNZ", "Kiwibank", "Westpac")
T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
TD = ProductType.TERM_DEPOSIT
HL = ProductType.HOME_LOAN
REF_10K = DepositReferencePolicy()

#: The audit's tiered ASB page (docs/audits/2026-11-13-evidence/ingest/td_tiers_and_legacy.py).
TIERED_ASB = """<html><body><table><tr><th>Term</th><th>$10,000 - $499,999</th>
<th>$500,000 - $5,000,000</th></tr>
<tr><td>6 months</td><td>3.60%</td><td>3.85%</td></tr>
<tr><td>12 months</td><td>4.00%</td><td>4.25%</td></tr></table></body></html>"""


def td(
    bank: str,
    rate: float,
    term: int = 12,
    *,
    freq: str = "at_maturity",
    min_bal: float | None = 1_000.0,
    max_bal: float | None = None,
    category: str = "term_deposit",
    name: str = "Term Deposit",
    observed: datetime = T0,
) -> CompetitorRateRecord:
    return CompetitorRateRecord(
        bank=bank,
        product_type="term_deposit",
        product_name=name,
        rate_category=category,
        term_label_raw=f"{term} months",
        term_months_normalised=term,
        advertised_rate_pct=rate,
        interest_payment_frequency=freq,
        min_balance_or_deposit=min_bal,
        max_balance_or_deposit=max_bal,
        source_url=f"https://example.invalid/{bank.lower()}/td",
        captured_at_utc=observed,
        raw_text_context=f"{name} | {term} months | {rate}%",
        raw_html_sha256="b" * 64,
    )


def hl(bank: str, rate: float, text: str, term: int = 12) -> CompetitorRateRecord:
    record = CompetitorRateRecord(
        bank=bank,
        product_type="home_loan",
        product_name="Fixed",
        rate_category="fixed",
        term_label_raw=f"{term} months",
        term_months_normalised=term,
        advertised_rate_pct=rate,
        source_url=f"https://example.invalid/{bank.lower()}/hl",
        captured_at_utc=T0,
        raw_text_context=text,
        raw_html_sha256="c" * 64,
    )
    return COLLECTORS_BY_BANK[bank](fetcher=lambda url: b"").enrich_mortgage_record(record)


def ordinary(records, policy=REF_10K, **kwargs):
    return build_market_snapshot(
        records, product_type=TD, rate_category="term_deposit", deposit_reference=policy, **kwargs
    )


def market12(records, **kwargs):
    return ordinary(records, **kwargs)[12]


# --------------------------------------------------------------- balance tiers


def test_t01_10k_reference_excludes_500k_tier():
    asb = [
        td("ASB", 4.00, min_bal=10_000, max_bal=499_999),
        td("ASB", 4.25, min_bal=500_000, max_bal=5_000_000),
    ]
    assert market12(asb).rate_for("ASB") == 4.00


def test_t02_500k_reference_selects_eligible_high_tier():
    asb = [
        td("ASB", 4.00, min_bal=10_000, max_bal=499_999),
        td("ASB", 4.25, min_bal=500_000, max_bal=5_000_000),
    ]
    policy = DepositReferencePolicy(balance_amount=500_000)
    assert market12(asb, policy=policy).rate_for("ASB") == 4.25


def test_t03_minimum_above_reference_is_excluded_and_disclosed():
    snapshot = ordinary([td("ASB", 4.40, min_bal=20_000), td("BNZ", 4.00)])
    assert snapshot[12].rate_for("ASB") is None
    reasons = {(e.bank, e.reason) for e in snapshot.reference_exclusions}
    assert ("ASB", "balance_below_minimum") in reasons


def test_t04_maximum_below_reference_is_excluded():
    snapshot = ordinary([td("ASB", 4.40, min_bal=0, max_bal=5_000), td("BNZ", 4.00)])
    assert snapshot[12].rate_for("ASB") is None
    assert {e.reason for e in snapshot.reference_exclusions} == {"balance_above_maximum"}


def test_t05_tier_boundaries_are_inclusive():
    assert market12([td("ASB", 4.1, min_bal=10_000)]).rate_for("ASB") == 4.1
    assert market12([td("ASB", 4.1, min_bal=0, max_bal=10_000)]).rate_for("ASB") == 4.1
    snapshot = ordinary([td("ASB", 4.1, min_bal=10_000.01)])
    assert 12 not in snapshot


def test_t06_missing_balance_is_disclosed_never_confirmed():
    entry = market12([td("ANZ", 4.0, min_bal=None)]).entries[0]
    assert entry.balance_basis == "not_stated"
    assert entry.reference_eligibility_established is False
    assert "not established" in entry.eligibility_note.lower()

    strict = DepositReferencePolicy(admit_unstated_balance=False)
    assert 12 not in ordinary([td("ANZ", 4.0, min_bal=None)], policy=strict)


def test_t06_unstated_sibling_admitted_only_with_disclosure():
    records = [td("ANZ", 4.20, min_bal=None), td("ANZ", 4.00, min_bal=5_000)]
    entry = market12(records).entries[0]
    assert (entry.rate_pct, entry.balance_basis) == (4.20, "not_stated")
    assert entry.eligibility_note == "Eligibility not established: balance tier not published"
    strict = DepositReferencePolicy(admit_unstated_balance=False)
    assert market12(records, policy=strict).rate_for("ANZ") == 4.00


def test_t07_multiple_tiers_from_one_bank_count_once():
    records = [
        td("ASB", 3.9, min_bal=1_000, max_bal=9_999),
        td("ASB", 4.0, min_bal=10_000, max_bal=99_999),
        td("ASB", 4.1, min_bal=10_000, max_bal=499_999, name="Term Deposit Plus"),
        td("ASB", 4.3, min_bal=500_000),
    ]
    market = market12(records)
    assert market.bank_count == 1
    assert market.rate_for("ASB") == 4.1


def test_t08_higher_rate_wins_only_among_eligible_tiers():
    records = [td("ASB", 4.05, min_bal=5_000), td("ASB", 4.60, min_bal=250_000)]
    assert market12(records).rate_for("ASB") == 4.05


def _four_bank_market():
    return [
        td("ASB", 4.00, min_bal=10_000, max_bal=499_999),
        td("ASB", 4.75, min_bal=500_000),
        td("ANZ", 3.90),
        td("BNZ", 4.05),
        td("Kiwibank", 4.10),
    ]


def test_t09_median_uses_comparable_bank_rates():
    assert market12(_four_bank_market()).median_rate == pytest.approx(4.025)


def test_t10_leader_excludes_ineligible_tiers():
    market = market12(_four_bank_market())
    assert market.market_leader == "Kiwibank"
    assert market.best_rate == 4.10


# ----------------------------------------------------------- payment frequency


def test_t11_preferred_at_maturity_selected_over_higher_monthly():
    records = [td("ASB", 4.30, freq="monthly"), td("ASB", 4.00, freq="at_maturity")]
    entry = market12(records).entries[0]
    assert (entry.rate_pct, entry.frequency_basis, entry.interest_payment_frequency) == (
        4.00,
        "published",
        "at_maturity",
    )


def test_t12_monthly_only_rate_is_incompatible_by_default():
    snapshot = ordinary([td("ASB", 4.30, freq="monthly"), td("BNZ", 4.00)])
    assert snapshot[12].rate_for("ASB") is None
    assert {e.reason for e in snapshot.reference_exclusions} == {"frequency_incompatible"}


def test_t12_explicit_fallback_frequency_is_disclosed():
    policy = DepositReferencePolicy(fallback_frequencies=("monthly",))
    entry = market12([td("ASB", 4.30, freq="monthly")], policy=policy).entries[0]
    assert entry.frequency_basis == "fallback"
    assert entry.reference_eligibility_established is False


def test_t13_multi_option_frequency_offers_at_maturity():
    entry = market12([td("ASB", 4.10, freq="at_maturity+monthly")]).entries[0]
    assert entry.frequency_basis == "published"
    assert entry.interest_payment_frequency == "at_maturity+monthly"


def test_t14_unknown_frequency_never_labelled_at_maturity():
    entry = market12([td("ANZ", 4.10, freq="unknown")]).entries[0]
    assert entry.frequency_basis == "not_stated"
    assert entry.interest_payment_frequency == "unknown"
    assert entry.reference_eligibility_established is False

    records = [td("ANZ", 4.40, freq="unknown"), td("ANZ", 4.00, freq="at_maturity")]
    assert market12(records).rate_for("ANZ") == 4.00

    strict = DepositReferencePolicy(admit_unstated_frequency=False)
    assert 12 not in ordinary([td("ANZ", 4.10, freq="unknown")], policy=strict)


def test_t15_selection_independent_of_row_order():
    records = [
        td("ASB", 4.10, name="Term Deposit A", freq="at_maturity+monthly"),
        td("ASB", 4.10, name="Term Deposit B"),
        td("ASB", 4.20, freq="unknown"),
        td("ASB", 4.50, min_bal=500_000),
    ]
    forward = market12(records).entries[0]
    backward = market12(list(reversed(records))).entries[0]
    assert forward == backward
    assert forward.product_name == "Term Deposit A"


def test_t15_home_loan_tie_break_independent_of_row_order(make_record):
    a = make_record(bank="ANZ", rate_pct=4.79, product_name="Fixed Special A")
    b = make_record(bank="ANZ", rate_pct=4.79, product_name="Fixed Special B", max_lvr=0.8)
    forward = build_market_snapshot([a, b])[12].entries[0]
    backward = build_market_snapshot([b, a])[12].entries[0]
    assert forward == backward


def test_t16_pie_never_enters_ordinary_benchmark():
    records = [td("ANZ", 4.00), td("ANZ", 5.10, category="pie_term_deposit")]
    assert market12(records).rate_for("ANZ") == 4.00


def test_t17_comparable_special_still_respects_reference_tier():
    special = td("ASB", 4.60, category="special", min_bal=500_000)
    records = [td("ASB", 4.00, min_bal=10_000), special]
    snapshot = ordinary(records, admitted_keys={special.observation_key()})
    entry = snapshot[12].entries[0]
    assert (entry.rate_pct, entry.rate_category) == (4.00, "term_deposit")

    retail_special = td("ASB", 4.30, category="special", min_bal=5_000)
    snapshot = ordinary(
        [records[0], retail_special], admitted_keys={retail_special.observation_key()}
    )
    assert snapshot[12].entries[0].rate_category == "special"


# --------------------------------------------------------- service-level (SQLite)


class Market:
    """Five-bank TD market from the 2026-10-30 captures; ASB's page is replaceable."""

    def __init__(self, tmp_path: Path, asb_html: str, **config) -> None:
        self.pages = {
            COLLECTORS_BY_BANK[bank].term_deposit_url: (
                LIVE_TD / f"{bank.lower()}_term_deposits.html"
            ).read_text()
            for bank in BANKS
        }
        self.pages[COLLECTORS_BY_BANK["ASB"].term_deposit_url] = asb_html
        db = str(tmp_path / "obs.db")
        self.config = PricingIntelligenceConfig(
            database_path=db,
            collection=CollectionConfig(inter_request_delay_seconds=0, max_retries=0),
            **config,
        )
        collectors = build_collectors(
            banks=self.config.selected_banks,
            config=self.config.collection,
            fetcher=lambda url: self.pages[url].encode("utf-8"),
            raw_root=str(tmp_path / "raw"),
        )
        self.service = PricingIntelligenceService(
            config=self.config,
            store=ObservationStore(db),
            collectors=collectors,
            raw_root=str(tmp_path / "raw"),
        )

    def collect(self, at: datetime = T0, run_id: str | None = None):
        return run_operational_cycle(
            self.service,
            product_type="term_deposit",
            captured_at_utc=at,
            run_id=run_id,
            alert_sinks={},
        )

    def snapshot(self, **kwargs):
        return self.service.get_market_snapshot(
            product_type=TD, now=T0 + timedelta(hours=1), **kwargs
        )


def test_audit_reproduction_tiered_asb_10k_and_500k(tmp_path):
    market = Market(tmp_path / "a", TIERED_ASB)
    market.collect()
    at_10k = market.snapshot()[12]
    assert at_10k.rate_for("ASB") == 4.00
    assert at_10k.bank_count == 5
    assert at_10k.best_rate == max(at_10k.rates)

    big = Market(tmp_path / "b", TIERED_ASB, deposit_reference={"balance_amount": 500_000})
    big.collect()
    at_500k = big.snapshot()[12]
    assert at_500k.rate_for("ASB") == 4.25
    # Other banks keep their own representatives; leadership is computed, not assumed.
    assert at_500k.best_rate == max(at_500k.rates)
    for bank in ("ANZ", "BNZ", "Kiwibank", "Westpac"):
        assert at_500k.rate_for(bank) == at_10k.rate_for(bank)


def test_t37_raw_all_tier_view_retains_underlying_data(tmp_path):
    market = Market(tmp_path, TIERED_ASB)
    market.collect()
    raw = market.snapshot(all_tiers=True)
    assert raw.deposit_reference is None
    assert raw[12].rate_for("ASB") == 4.25
    unfiltered = market.snapshot(rate_category=None)
    assert unfiltered.deposit_reference is None
    default = market.snapshot()
    assert default.deposit_reference == market.config.deposit_reference
    # Storage keeps both tiers: the filter is a view, not a deletion.
    stored = [
        r.advertised_rate_pct
        for r in market.service.store.get_current_observations(product_type=TD)
        if r.bank == "ASB" and r.term_months_normalised == 12
    ]
    assert sorted(stored) == [4.00, 4.25]


def test_t38_as_of_snapshot_reproducible(tmp_path):
    market = Market(tmp_path, TIERED_ASB)
    run_id = "phase-2f-run-1"
    market.collect(run_id=run_id)
    before = market.snapshot(as_of_run_id=run_id)[12]
    market.pages[COLLECTORS_BY_BANK["ASB"].term_deposit_url] = TIERED_ASB.replace(
        "4.00%", "4.05%"
    )
    market.collect(T0 + timedelta(days=1))
    again = market.snapshot(as_of_run_id=run_id)[12]
    assert again.rate_for("ASB") == before.rate_for("ASB") == 4.00


def test_t20_quarantined_rate_stays_out_of_reference_view(tmp_path):
    market = Market(tmp_path, TIERED_ASB)
    market.collect()
    jumped = TIERED_ASB.replace("4.00%", "5.50%")
    market.pages[COLLECTORS_BY_BANK["ASB"].term_deposit_url] = jumped
    market.collect(T0 + timedelta(minutes=30))
    assert market.snapshot()[12].rate_for("ASB") == 4.00


# ------------------------------------------------------- mortgage eligibility


def test_t21_explicit_80_lvr_is_published():
    record = hl("Westpac", 4.89, "Fixed | 1 year | 4.89% | 80% LVR")
    assert (record.max_lvr, record.eligibility_inferred) == (0.8, False)
    assert record.lvr_eligibility_basis() == "published"


def test_t22_explicit_20_equity_is_published():
    record = hl("Westpac", 4.89, "Fixed | 1 year | 4.89% | with 20% equity")
    assert (record.min_equity, record.eligibility_inferred) == (0.2, False)


def test_t23_lvr_implied_by_special_label_is_inferred():
    record = hl("ANZ", 4.79, "Fixed Special | 1 year | 4.79%")
    assert (record.max_lvr, record.min_equity) == (0.8, 0.2)
    assert record.eligibility_inferred is True
    assert record.lvr_eligibility_basis() == "inferred"
    unmarked = hl("ASB", 4.89, "1 year | 4.89%")
    assert unmarked.eligibility_inferred is True


def test_t24_unknown_lvr_stays_unknown():
    record = hl("Westpac", 5.49, "Fixed | 1 year | 5.49%")
    assert record.max_lvr is None and record.min_equity is None
    assert record.eligibility_inferred is False
    assert record.lvr_eligibility_basis() == "unknown"


def test_t25_observation_identity_unchanged_by_provenance():
    record = hl("ANZ", 4.79, "Fixed Special | 1 year | 4.79%")
    assert record.observation_key() == (
        "ANZ|home_loan|special|12|unknown|fixed|||min_20_percent_equity|0.8000|0.2000"
    )


def test_t26_eligibility_never_leaks_from_another_row():
    html = """<html><body><table><tr><th>Term</th><th>Rate</th><th>Notes</th></tr>
    <tr><td>1 year</td><td>4.79%</td><td>80% LVR</td></tr>
    <tr><td>2 years</td><td>4.99%</td><td>Fixed</td></tr></table></body></html>"""
    records = COLLECTORS_BY_BANK["Westpac"](fetcher=lambda url: b"").parse_mortgage_html(html, T0)
    by_term = {r.term_months_normalised: r for r in records}
    assert by_term[12].max_lvr == 0.8 and by_term[12].eligibility_inferred is False
    assert by_term[24].max_lvr is None and by_term[24].eligibility_inferred is False


def test_t27_legacy_stored_and_serialised_records_derive_provenance(tmp_path):
    inferred = hl("ANZ", 4.79, "Fixed Special | 1 year | 4.79%")
    payload = inferred.model_dump()
    payload.pop("eligibility_inferred")
    assert CompetitorRateRecord.model_validate(payload).eligibility_inferred is True
    # A claimed "not inferred" cannot override the evidence-derived value.
    payload["eligibility_inferred"] = False
    assert CompetitorRateRecord.model_validate(payload).eligibility_inferred is True

    store = ObservationStore(str(tmp_path / "obs.db"))
    # The observations table has no provenance column: it is derived from the stored row text.
    store.record_observations([inferred], run_id="r1", observed_at=T0, product_type=HL)
    [read_back] = store.get_current_observations(product_type=HL)
    assert read_back.eligibility_inferred is True
    assert read_back.observation_key() == inferred.observation_key()


def test_t28_inferred_eligibility_visible_in_market_evidence():
    records = [
        hl("ANZ", 4.79, "Fixed Special | 1 year | 4.79%"),
        hl("Westpac", 4.89, "Fixed Special | 1 year | 4.89% | 80% LVR"),
        hl("Kiwibank", 4.95, "Fixed Special | 1 year | 4.95% | 80% LVR"),
    ]
    snapshot = build_market_snapshot(records)
    entry = snapshot[12].entries[0]
    assert entry.bank == "ANZ" and entry.eligibility_inferred is True
    assert entry.eligibility_note == "80% max LVR — inferred from special-rate category"
    published = snapshot[12].entries[1]
    assert published.eligibility_note == "80% max LVR — published eligibility"
    resolved = resolve_competitor_rate("MARKET_LEADER", snapshot, term_months=12, now=T0)
    assert "inferred" in resolved.rationale and "ANZ" in resolved.rationale


@pytest.mark.parametrize(
    ("bank", "text", "basis"),
    [
        ("Westpac", "Fixed Special | 12 months | 4.89% | 80% LVR", "published"),
        ("ANZ", "Fixed Special | 12 months | 4.89%", "inferred"),
        ("ASB", "12 months | 4.89%", "inferred"),
        ("Westpac", "Fixed | 12 months | 4.89%", "unknown"),
    ],
)
def test_historical_lvr_recovers_original_capture(tmp_path, bank, text, basis):
    record = hl(bank, 4.89, text)
    path = tmp_path / "obs.db"
    with ObservationStore(path) as store:
        store.record_observations([record], run_id="first", observed_at=T0)
    # Open the existing file read-only to exercise the same storage contract as the dashboard.
    with ObservationStore.open_reader(path) as store:
        [current] = store.get_current_observations()
        [historical] = store.get_observations_as_of(T0, before_run_id="first")
        [confirmed] = store.get_confirmed_observations_as_of(T0, before_run_id="first")
        [history] = store.get_rate_history()
        for restored in (current, historical, confirmed, history):
            assert restored.observation_key() == record.observation_key()
            assert restored.lvr_eligibility_basis() == basis
            assert restored.eligibility_inferred is (basis == "inferred")


@pytest.mark.parametrize("elapsed", [timedelta(0), timedelta(hours=1)])
def test_historical_lvr_does_not_use_evidence_from_later_touch(tmp_path, elapsed):
    observed = hl("ASB", 4.89, "12 months | 4.89%")
    recapture = observed.model_copy(
        update={"raw_text_context": "12 months | 4.89% | Maximum 80% LVR"}
    )
    with ObservationStore(tmp_path / "obs.db") as store:
        store.record_observations([observed], run_id="first", observed_at=T0)
        store.record_observations([recapture], run_id="later", observed_at=T0 + elapsed)
        [history] = store.get_rate_history()
        assert (history.max_lvr, history.min_equity) == (0.8, 0.2)
        assert history.lvr_eligibility_basis() == "unknown"
        for read_as_of in (store.get_observations_as_of, store.get_confirmed_observations_as_of):
            [earlier] = read_as_of(T0, before_run_id="first")
            [later] = read_as_of(T0 + elapsed, before_run_id="later")
            [by_time] = read_as_of(T0)
            assert earlier.lvr_eligibility_basis() == "unknown"
            assert earlier.eligibility_inferred is False
            assert "Maximum 80%" not in earlier.raw_text_context
            assert earlier.captured_at_utc == later.captured_at_utc == T0
            assert later.lvr_eligibility_basis() == "published"
            assert by_time.lvr_eligibility_basis() == ("unknown" if elapsed else "published")


@pytest.mark.parametrize(
    ("bank", "notes", "basis"),
    [("Westpac", "80% LVR", "published"), ("ANZ", "Special", "inferred")],
)
def test_historical_lvr_uses_recaptured_collector_text_only_at_its_cutoff(
    tmp_path, bank, notes, basis
):
    html = f"""<table><tr><th>Term</th><th>Rate</th><th>Notes</th></tr>
    <tr><td>1 year</td><td>4.89%</td><td>{notes}</td></tr></table>"""
    collector = COLLECTORS_BY_BANK[bank](fetcher=lambda url: b"")
    [record] = collector.parse_mortgage_html(html, T0)
    assert record.special_conditions == record.raw_text_context
    later = T0 + timedelta(hours=1)
    with ObservationStore(tmp_path / "obs.db") as store:
        store.record_observations([record], run_id="first", observed_at=T0)
        [first] = store.get_observations_as_of(T0, before_run_id="first")
        assert first.lvr_eligibility_basis() == basis
        store.record_observations([record], run_id="later", observed_at=later)
        [first] = store.get_observations_as_of(T0, before_run_id="first")
        [recaptured] = store.get_observations_as_of(later, before_run_id="later")
        assert first.lvr_eligibility_basis() == "unknown"
        assert recaptured.lvr_eligibility_basis() == basis


def test_later_text_does_not_validate_legacy_eligibility_metadata(tmp_path):
    published = "12 months | 4.89% | Maximum 80% LVR"
    record = hl("ASB", 4.89, "12 months | 4.89%").model_copy(
        update={"special_conditions": published}
    )
    with ObservationStore(tmp_path / "obs.db") as store:
        store.record_observations([record], run_id="first", observed_at=T0)
        store.record_observations(
            [record.model_copy(update={"raw_text_context": published})],
            run_id="later",
            observed_at=T0 + timedelta(hours=1),
        )
        [historical] = store.get_observations_as_of(T0, before_run_id="first")
        assert historical.lvr_eligibility_basis() == "unknown"
        assert published != historical.raw_text_context


@pytest.mark.parametrize(
    "text", ["", "Fixed | 12 months | 4.89%", "Fixed Special | 12 months | 4.89% | 75% LVR"]
)
def test_legacy_lvr_needs_supporting_evidence(text):
    record = hl("Westpac", 4.89, "Fixed | 12 months | 4.89% | 80% LVR")
    payload = record.model_dump()
    payload.update(
        raw_text_context=text,
        special_conditions=record.raw_text_context,
        lvr_basis="published",
        lvr_eligibility_basis="published",
        eligibility_inferred=True,
    )
    restored = CompetitorRateRecord.model_validate(payload)
    assert restored.observation_key() == record.observation_key()
    assert restored.lvr_eligibility_basis() == "unknown"
    assert restored.eligibility_inferred is False


@pytest.mark.parametrize(
    ("text", "basis"),
    [
        ("12 months | 4.89% | Minimum equity: 20%", "published"),
        ("12 months | 4.89% | Maximum LVR: 80%", "published"),
        ("12 months | 4.89% | No 80% LVR requirement", "unknown"),
        ("12 months | 4.89% | No minimum equity of 20% is required", "unknown"),
        ("12 months | 4.89% | An LVR of 80% is not required", "unknown"),
        ("12 months | 4.89% | 120% equity", "unknown"),
        ("N/A", "unknown"),
    ],
)
def test_lvr_distinguishes_explicit_bounds_from_placeholders_and_negations(text, basis):
    record = hl("ASB", 4.89, "12 months | 4.89%")
    assert record.model_copy(update={"raw_text_context": text}).lvr_eligibility_basis() == basis


def test_published_legacy_equity_only_uses_the_stored_bound_in_the_label():
    record = hl("Westpac", 4.89, "Fixed Special | 12 months | 4.89% | with 20% equity")
    equity_only = record.model_copy(update={"max_lvr": None})
    assert build_market_snapshot([equity_only])[12].entries[0].eligibility_note == (
        "20% min equity — published eligibility"
    )


def test_lvr_provenance_in_previous_as_of_and_current_market_evidence(tmp_path):
    from balance_pricing_engine.pricing_intelligence.dashboard_data import build_ranking_frame

    records = [
        hl("ASB", 4.79, "12 months | 4.79%"),
        hl("ANZ", 4.85, "Fixed Special | 12 months | 4.85%"),
        hl("Westpac", 4.89, "Fixed Special | 12 months | 4.89% | 80% LVR"),
    ]
    now = T0 + timedelta(hours=1)
    with ObservationStore(tmp_path / "obs.db") as store:
        store.record_observations(records, run_id="first", observed_at=T0)
        store.record_observations([records[0]], run_id="later", observed_at=now)
        service = PricingIntelligenceService(config=PricingIntelligenceConfig(), store=store)
        previous = service.get_previous_market_snapshot()
        as_of = service.get_market_snapshot(as_of_run_id="first", now=now)
        current = service.get_market_snapshot(now=now)

        assert previous == as_of
        assert as_of.evaluated_at == T0
        expected = {
            "ASB": "80% max LVR — eligibility provenance unavailable",
            "ANZ": "80% max LVR — inferred from special-rate category",
            "Westpac": "80% max LVR — published eligibility",
        }
        assert {entry.bank: entry.eligibility_note for entry in as_of[12].entries} == expected
        assert build_ranking_frame(as_of, 12).set_index("Bank")["Eligibility"].to_dict() == expected
        assert current[12].entries[0].eligibility_inferred
        historical_rate = resolve_competitor_rate("MARKET_MEDIAN", as_of, term_months=12)
        current_rate = resolve_competitor_rate("MARKET_MEDIAN", current, term_months=12)
        assert historical_rate.competitor_loan_rate == current_rate.competitor_loan_rate
        assert historical_rate.coverage.oldest_input_age_hours == 0
        assert historical_rate.coverage.eligible_bank_count == 3
        assert historical_rate.coverage.eligibility_caveat_banks == ("ANZ", "ASB")
        assert "special-rate category, not published: ANZ" in historical_rate.rationale
        assert "LVR eligibility not established for ASB" in historical_rate.rationale
        assert "eligibility inferred or not established for ANZ, ASB" in (
            historical_rate.evidence_summary()
        )


# ---------------------------------------------------------- own-bank identity


def test_t29_t30_aliases_resolve_to_canonical_bank():
    assert canonical_bank("ASB") == "ASB"
    assert canonical_bank("ASB Bank") == "ASB"
    assert canonical_bank("  asb   bank ") == "ASB"
    assert canonical_bank("ANZ Bank New Zealand") == "ANZ"
    assert canonical_bank("Bank of New Zealand") == "BNZ"
    assert same_bank("ASB Bank", "ASB")
    assert PricingIntelligenceConfig(our_bank="ASB Bank").our_bank == "ASB"
    assert PricingIntelligenceConfig(our_bank=None).our_bank is None


@pytest.mark.parametrize("value", ["ASB Banking Group", "Heartland", "AS B", "", "  "])
def test_t31_unknown_our_bank_fails_validation(value):
    with pytest.raises(ValidationError, match="our_bank"):
        PricingIntelligenceConfig(our_bank=value)
    with pytest.raises(UnknownBankError):
        canonical_bank(value)


def test_t32_selected_bank_aliases_cannot_duplicate_institution():
    config = PricingIntelligenceConfig(selected_banks=["ASB Bank", "anz"])
    assert config.selected_banks == ["ASB", "ANZ"]
    with pytest.raises(ValidationError, match="ASB"):
        PricingIntelligenceConfig(selected_banks=["ASB", "ASB Bank"])
    with pytest.raises(ValidationError):
        PricingIntelligenceConfig(selected_banks=["ASB", "Heartland"])
    legacy = CompetitorRatesConfig(selected_banks=["ASB Bank"], term_deposit={"bank": "asb"})
    assert legacy.selected_banks == ["ASB"] and legacy.term_deposit.bank == "ASB"


def _td_market_snapshot():
    return ordinary(
        [td("ASB", 4.30), td("ANZ", 3.90), td("BNZ", 4.05), td("Kiwibank", 4.10)]
    )


def test_t33_own_bank_alias_reflected_in_min_banks():
    snapshot = _td_market_snapshot()
    canonical = assess_benchmark_coverage(snapshot, 12, min_banks=4, our_bank="ASB")
    alias = assess_benchmark_coverage(snapshot, 12, min_banks=4, our_bank="ASB Bank")
    assert canonical == alias
    assert alias.eligible_banks == ("ANZ", "BNZ", "Kiwibank")
    assert not alias.sufficient and alias.own_bank_excluded


def test_t34_home_loan_strategy_excludes_own_bank_alias(make_record):
    snapshot = build_market_snapshot(
        [
            make_record(bank=bank, rate_pct=rate)
            for bank, rate in [("ASB", 4.59), ("ANZ", 4.79), ("BNZ", 4.85), ("Westpac", 4.89)]
        ]
    )
    results = [
        resolve_competitor_rate("MARKET_LEADER", snapshot, 12, our_bank=name, now=T0)
        for name in ("ASB", "ASB Bank")
    ]
    assert results[0] == results[1]
    assert results[0].rate_pct == 4.79 and "ASB" not in results[0].source_banks


def test_t35_term_deposit_strategy_excludes_own_bank_alias():
    snapshot = _td_market_snapshot()
    results = [
        resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12, our_bank=name, now=T0)
        for name in ("ASB", "ASB Bank")
    ]
    assert results[0] == results[1]
    assert results[0].rate_pct == 4.05


def test_t36_joint_overlay_excludes_own_bank_on_each_side(make_record):
    base = load_config("configs/base.yaml")
    loans = build_market_snapshot(
        [
            make_record(bank=bank, rate_pct=rate)
            for bank, rate in [("ASB", 4.59), ("ANZ", 4.79), ("BNZ", 4.85), ("Westpac", 4.89)]
        ]
    )
    deposits = _td_market_snapshot()
    overlays = []
    for name in ("ASB", "ASB Bank"):
        config = base.model_copy(
            update={"pricing_intelligence": PricingIntelligenceConfig(our_bank=name)}
        )
        overlays.append(
            resolve_market_overlay(
                config,
                strategy="MARKET_LEADER",
                snapshot=loans,
                deposit_snapshot=deposits,
                deposit_strategy="MARKET_LEADER",
                now=T0,
            )
        )
    assert overlays[0].loan == overlays[1].loan
    assert overlays[0].deposit == overlays[1].deposit
    assert overlays[0].loan.rate_pct == 4.79
    assert overlays[0].deposit.rate_pct == 4.10


# ------------------------------------------------------------- config lineage


@pytest.mark.parametrize(
    "bad",
    [
        {"balance_amount": 0},
        {"balance_amount": -1},
        {"balance_amount": float("inf")},
        {"balance_amount": float("nan")},
        {"interest_payment_frequency": "unknown"},
        {"interest_payment_frequency": "weekly"},
        {"fallback_frequencies": ("at_maturity",)},
        {"fallback_frequencies": ("monthly", "monthly")},
    ],
)
def test_reference_policy_validation(bad):
    with pytest.raises(ValidationError):
        DepositReferencePolicy(**bad)


def test_t39_reference_policy_changes_effective_config_hash():
    base = load_config("configs/base.yaml")
    default = base.model_copy(update={"pricing_intelligence": PricingIntelligenceConfig()})
    large = base.model_copy(
        update={
            "pricing_intelligence": PricingIntelligenceConfig(
                deposit_reference={"balance_amount": 500_000}
            )
        }
    )
    assert effective_config_hash(default) != effective_config_hash(large)
    assert effective_config_hash(default) == effective_config_hash(
        base.model_copy(update={"pricing_intelligence": PricingIntelligenceConfig()})
    )


# ------------------------------------------------------- evidence surfaces


def test_t28_dashboard_positions_carry_backend_eligibility_label(tmp_path):
    from balance_pricing_engine.dashboard.services import DashboardServices
    from balance_pricing_engine.dashboard.settings import load_settings

    market = Market(tmp_path, TIERED_ASB)
    market.collect()
    services = DashboardServices(
        load_settings({}, database_path=market.config.database_path),
        clock=lambda: T0 + timedelta(hours=1),
    )
    positions = {p.bank: p for p in services.bank_positions(12, TD).value}
    assert positions["ASB"].rate_pct == 4.00
    assert positions["ANZ"].eligibility_note.startswith("Eligibility not established")
    assert positions["Westpac"].eligibility_note == (
        "Reference balance tier and payment frequency published"
    )


def test_coverage_and_rationale_disclose_unestablished_reference_eligibility():
    snapshot = ordinary(
        [td("ANZ", 3.90, freq="unknown", min_bal=None), td("BNZ", 4.05), td("Kiwibank", 4.10)]
    )
    resolved = resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12, now=T0)
    assert "eligibility inferred or not established for ANZ" in resolved.evidence_summary()
    assert "reference-depositor eligibility not established" in resolved.rationale


def test_alias_registry_comes_from_collectors_and_is_unambiguous():
    from balance_pricing_engine.competitor_rates.bank_identity import canonical_banks

    assert canonical_banks() == tuple(sorted(COLLECTORS_BY_BANK))
    for bank, collector in COLLECTORS_BY_BANK.items():
        for alias in collector.bank_aliases:
            assert canonical_bank(alias) == bank

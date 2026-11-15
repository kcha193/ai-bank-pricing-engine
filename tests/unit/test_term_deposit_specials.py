"""Phase 2b: term-deposit specials and extraction completeness (DATA-001, DATA-019).

The fixtures under ``tests/fixtures/pricing_intelligence/term_deposit_specials/`` are synthetic
(invented rates and wording). The other four banks use the trimmed live captures from
2026-10-30, so the market around ASB is realistic: BNZ and Kiwibank lead 12 months at 4.05%.

Policy under test (see ``pricing_intelligence.deposit_specials``): a term-deposit special is
admitted to the ordinary term-deposit benchmark only with explicit evidence of public
availability and nothing that makes it unlike the bank's ordinary rate. A special with
disqualifying evidence is recorded as not comparable. Anything in between is *uncertain*: it
is kept and tagged, never promoted, and the run is degraded (exit 2) instead of reporting
success.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    COLLECTORS_BY_BANK,
    build_collectors,
)
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.competitor_rates.normalise import normalise_balance
from balance_pricing_engine.pricing_intelligence.changes import ChangeType
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.deposit_specials import (
    SpecialComparability,
    assess_deposit_specials,
)
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    build_summary,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.quarantine import ReviewAction, ReviewState
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pricing_intelligence"
SPECIALS = FIXTURES / "term_deposit_specials"
LIVE = FIXTURES / "term_deposits_2026-10-30"
BANKS = ("ANZ", "ASB", "BNZ", "Kiwibank", "Westpac")
T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
TD = ProductType.TERM_DEPOSIT


def fixture(name: str) -> str:
    return (SPECIALS / name).read_text()


def parse_asb(html: str, skipped: list | None = None) -> list[CompetitorRateRecord]:
    collector = COLLECTORS_BY_BANK["ASB"](fetcher=lambda url: b"")
    return collector.parse_term_deposit_html(html, T0, skipped_row_keys=skipped)


class Market:
    """Five-bank TD market from fixtures; ASB's page is swappable between runs."""

    def __init__(self, tmp_path: Path, asb_html: str) -> None:
        self.pages = {
            COLLECTORS_BY_BANK[bank].term_deposit_url: (
                LIVE / f"{bank.lower()}_term_deposits.html"
            ).read_text()
            for bank in BANKS
        }
        self.set_asb(asb_html)
        db = str(tmp_path / "obs.db")
        self.config = PricingIntelligenceConfig(
            database_path=db,
            collection=CollectionConfig(inter_request_delay_seconds=0, max_retries=0),
        )
        self.store = ObservationStore(db)
        collectors = build_collectors(
            banks=self.config.selected_banks,
            config=self.config.collection,
            fetcher=lambda url: self.pages[url].encode("utf-8"),
            raw_root=str(tmp_path / "raw"),
        )
        self.service = PricingIntelligenceService(
            config=self.config, store=self.store, collectors=collectors,
            raw_root=str(tmp_path / "raw"),
        )  # fmt: skip

    def set_asb(self, html: str) -> None:
        self.pages[COLLECTORS_BY_BANK["ASB"].term_deposit_url] = html

    @staticmethod
    def when(day: int, hours: int = 0) -> datetime:
        return T0 + timedelta(days=day, hours=hours)

    def run(self, day: int):
        return run_operational_cycle(
            self.service, product_type=TD, captured_at_utc=self.when(day),
            run_id=f"run-{day}", alert_sinks={},
        )  # fmt: skip

    def term12(self, at: datetime):
        return self.service.get_market_snapshot(product_type=TD, now=at).term(12)

    def asb12(self, at: datetime):
        entry = next(e for e in self.term12(at).entries if e.bank == "ASB")
        return entry.rate_pct, entry.rate_category


def summary_of(result) -> dict:
    return build_summary(result.outcome, TD, quarantine=result.quarantine)


def specials_in(result) -> dict[float, tuple[str, tuple[str, ...]]]:
    return {
        a.rate_pct: (a.status.value, a.reasons) for a in result.outcome.deposit_special_assessments
    }


# ------------------------------------------------------------------- extraction (T01, T02)


def test_t01_td_special_row_is_parsed_and_preserved_with_provenance():
    (special,) = [r for r in parse_asb(fixture("b_special_same_term.html")) if r.rate_category == "special"]
    assert (special.term_months_normalised, special.advertised_rate_pct) == (12, 4.50)
    assert special.min_balance_or_deposit == 5000.0
    assert special.special_conditions and "Special" in special.special_conditions
    assert special.source_url == COLLECTORS_BY_BANK["ASB"].term_deposit_url
    assert len(special.raw_html_sha256) == 64 and special.parser_version


def test_t02_special_and_standard_rows_keep_distinct_identities():
    records = [r for r in parse_asb(fixture("b_special_same_term.html")) if r.term_months_normalised == 12]
    assert sorted(r.rate_category for r in records) == ["special", "term_deposit"]
    assert len({r.observation_key() for r in records}) == 2


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # C: separate table, special named only in the column heading.
        ("c_special_separate_table.html", [(12, 4.45)]),
        # D: standard and special side by side under their own headings.
        ("d_special_column_heading.html", [(6, 3.85), (12, 4.40)]),
        # E: the marker is part of the term label itself.
        ("e_special_in_term_label.html", [(9, 4.10)]),
    ],
)
def test_specials_are_recognised_in_every_layout(name, expected):
    specials = [
        (r.term_months_normalised, r.advertised_rate_pct)
        for r in parse_asb(fixture(name))
        if r.rate_category == "special"
    ]
    assert specials == expected


# ------------------------------------------------------- comparability policy (T03-T05)


def test_t03_publicly_available_special_enters_the_benchmark_and_leads(tmp_path):
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    result = market.run(0)
    assert result.code == EXIT_OK
    assert specials_in(result)[4.45][0] == "comparable"
    term = market.term12(market.when(0, 1))
    assert term.best_rate == 4.45 and term.market_leaders == ("ASB",)
    assert market.asb12(market.when(0, 1)) == (4.45, "special")  # classification preserved
    lead = resolve_competitor_rate("MARKET_LEADER", market.service.get_market_snapshot(
        product_type=TD, now=market.when(0, 1)), 12)  # fmt: skip
    assert lead.rate_pct == 4.45 and "special" in lead.rationale


def test_t04_restricted_special_is_recorded_but_not_promoted(tmp_path):
    market = Market(tmp_path, fixture("p_restricted_special.html"))
    result = market.run(0)
    status, reasons = specials_in(result)[4.75]
    assert status == "not_comparable"
    assert any("new money" in reason for reason in reasons)
    assert result.code == EXIT_OK  # an understood exclusion is not a degraded collection
    assert market.asb12(market.when(0, 1)) == (4.00, "term_deposit")
    assert market.term12(market.when(0, 1)).best_rate == 4.05


def test_t05_t26_audit_replay_uncertain_special_degrades_the_run(tmp_path):
    """The audit's td_special_mixed.py replay: ASB 12m 4.00% standard plus 4.50% special."""
    live_asb = (LIVE / "asb_term_deposits.html").read_text()
    asb = live_asb.replace(
        "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>",
        "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>\n"
        "  <tr><td>12 Months Special</td><td>4.50% p.a.</td></tr>",
    )
    assert asb != live_asb
    market = Market(tmp_path, asb)
    result = market.run(0)
    summary = summary_of(result)
    # Before: exit 0 SUCCESS, failures [] and the 4.50% special silently ignored.
    assert result.code == EXIT_PARTIAL_FAILURE and result.status == "PARTIAL_SUCCESS"
    status, reasons = specials_in(result)[4.50]
    assert status == "uncertain" and "no_public_availability_evidence" in reasons
    assert [f["error_type"] for f in summary["failures"]] == ["special_comparability_uncertain"]
    assert summary["bank_extraction_status"]["ASB"] == "COMPARABILITY_UNCERTAIN"
    assert "ASB" in summary["banks_succeeded"]  # the collector itself worked
    # Not silently promoted: the benchmark stays on the trusted ordinary rates.
    assert market.term12(market.when(0, 1)).best_rate == 4.05
    assert market.asb12(market.when(0, 1)) == (4.00, "term_deposit")
    # Still stored, tagged and auditable.
    stored = market.store.get_current_observations(product_type=TD)
    assert any(r.bank == "ASB" and r.rate_category == "special" and r.advertised_rate_pct == 4.50
               for r in stored)  # fmt: skip


@pytest.mark.parametrize(
    ("name", "rate", "reason"),
    [
        ("f_special_minimum_balance.html", 4.60, "balance_tier_differs_from_standard"),
    ],
)
def test_special_with_disqualifying_evidence_is_not_comparable(tmp_path, name, rate, reason):
    market = Market(tmp_path, fixture(name))
    result = market.run(0)
    status, reasons = specials_in(result)[rate]
    assert status == "not_comparable" and reason in reasons
    assert market.term12(market.when(0, 1)).best_rate == 4.05


def test_special_without_a_standard_rate_at_its_term_still_needs_availability_evidence(tmp_path):
    market = Market(tmp_path, fixture("e_special_in_term_label.html"))
    result = market.run(0)
    assert specials_in(result)[4.10][0] == "uncertain"
    assert result.code == EXIT_PARTIAL_FAILURE


def _td(bank="ASB", rate=4.0, category="term_deposit", context="", **overrides):
    payload = dict(
        bank=bank, product_type="term_deposit", product_name="Term deposit",
        rate_category=category, term_label_raw="12 Months", term_months_normalised=12,
        advertised_rate_pct=rate, source_url="https://example.invalid/td",
        captured_at_utc=T0, raw_text_context=context or f"12 Months | {rate}% p.a.",
        raw_html_sha256="a" * 64,
    )  # fmt: skip
    payload.update(overrides)
    return CompetitorRateRecord(**payload)


@pytest.mark.parametrize(
    ("context", "overrides", "status", "reason"),
    [
        ("12 Months | 4.5% | Available to all customers", {}, "comparable",
         "public_availability_evidence:available to all"),
        ("12 Months | 4.5% | Available to all customers | PIE", {}, "not_comparable", "pie_marker"),
        ("12 Months | 4.5%* | Available to all customers", {}, "uncertain", "footnote_marker"),
        ("12 Months | 4.5% | Available to all customers | Conditions apply", {}, "not_comparable",
         "restriction_marker:conditions apply"),
        ("12 Months | 4.5% | Available to all customers | USD", {}, "not_comparable",
         "foreign_currency"),
        ("12 Months | 4.5% | Available to all customers", {"interest_payment_frequency": "monthly"},
         "uncertain", "payment_frequency_unverified"),
    ],
)
def test_assessment_rules(context, overrides, status, reason):
    standard = _td()
    special = _td(rate=4.5, category="special", context=context, **overrides)
    (assessment,) = assess_deposit_specials(
        [standard, special], restriction_markers=("conditions apply",),
        availability_markers=("available to all",), balance_tier_edges=(10_000.0, 100_000.0),
    )  # fmt: skip
    assert assessment.status is SpecialComparability(status)
    assert reason in assessment.reasons


def test_known_but_different_payment_frequency_is_not_comparable():
    standard = _td(interest_payment_frequency="at_maturity")
    special = _td(rate=4.5, category="special", interest_payment_frequency="monthly",
                  context="12 Months | 4.5% | Available to all customers")  # fmt: skip
    (assessment,) = assess_deposit_specials(
        [standard, special], restriction_markers=(), availability_markers=("available to all",),
        balance_tier_edges=(10_000.0,),
    )  # fmt: skip
    assert assessment.status is SpecialComparability.NOT_COMPARABLE
    assert "payment_frequency_differs_from_standard" in assessment.reasons


def test_policy_markers_are_configurable_from_yaml_shaped_input():
    config = PricingIntelligenceConfig(
        deposit_specials={"availability_markers": ["open to every depositor"]}
    )
    assert config.deposit_specials.availability_markers == ("open to every depositor",)
    with pytest.raises(ValueError):
        PricingIntelligenceConfig(deposit_specials={"availability_markers": ["  "]})


# ------------------------------------------------ partial extraction (T06-T08, T19, T20)


def test_t06_t07_t08_partial_page_degrades_without_fabricating_withdrawals(tmp_path):
    market = Market(tmp_path, fixture("a_standard_only.html"))
    assert market.run(0).code == EXIT_OK
    market.set_asb(fixture("m_partial_page.html"))
    result = market.run(1)
    summary = summary_of(result)
    assert result.code == EXIT_PARTIAL_FAILURE
    assert "ASB" in result.outcome.low_coverage_banks
    assert summary["bank_extraction_status"]["ASB"] == "PARSE_PARTIAL"
    assert not [c for c in result.outcome.changes if c.change_type is ChangeType.REMOVED_PRODUCT]
    # The valid 12-month row was stored; the unconfirmed ones keep their last-known state.
    asb = {r.term_months_normalised: r.advertised_rate_pct
           for r in market.store.get_current_observations(product_type=TD) if r.bank == "ASB"}  # fmt: skip
    assert asb == {6: 3.60, 12: 4.00, 24: 4.30, 36: 4.40}


def test_t19_layout_regression_between_runs_is_surfaced(tmp_path):
    market = Market(tmp_path, fixture("a_standard_only.html"))
    market.run(0)
    market.set_asb(fixture("o_layout_changed.html"))
    result = market.run(1)
    assert result.code == EXIT_PARTIAL_FAILURE
    assert summary_of(result)["bank_extraction_status"]["ASB"] == "PARSE_PARTIAL"
    assert not [c for c in result.outcome.changes if c.change_type is ChangeType.REMOVED_PRODUCT]


def test_recognised_special_row_with_malformed_rate_is_reported_not_dropped():
    skipped: list[str] = []
    records = parse_asb(fixture("n_special_unparseable.html"), skipped)
    assert not [r for r in records if r.rate_category == "special"]
    assert len(skipped) == 1 and "|12|" in skipped[0]


def test_t20_unparseable_special_on_a_first_collection_is_partial(tmp_path):
    market = Market(tmp_path, fixture("n_special_unparseable.html"))
    result = market.run(0)
    summary = summary_of(result)
    assert result.code == EXIT_PARTIAL_FAILURE
    assert summary["bank_extraction_status"]["ASB"] == "PARSE_PARTIAL"
    assert any(f["bank"] == "ASB" and f["error_type"] == "row_skipped" for f in summary["failures"])


def test_t20_special_that_becomes_unreadable_is_partial_not_withdrawn(tmp_path):
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    assert market.run(0).code == EXIT_OK
    market.set_asb(fixture("c_special_separate_table.html").replace("4.45% p.a.", "4.4S% p.a."))
    result = market.run(1)
    summary = summary_of(result)
    assert result.code == EXIT_PARTIAL_FAILURE
    assert summary["bank_extraction_status"]["ASB"] == "PARSE_PARTIAL"
    assert not [c for c in result.outcome.changes if c.change_type is ChangeType.REMOVED_PRODUCT]
    # The last trusted special is kept (under the usual freshness rules), not erased.
    assert market.asb12(market.when(1, 1)) == (4.45, "special")


def test_a_special_genuinely_removed_from_the_page_is_a_withdrawal_not_a_parser_fault(tmp_path):
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    market.run(0)
    market.set_asb(fixture("a_standard_only.html"))
    result = market.run(1)
    assert result.code == EXIT_OK
    removed = [c for c in result.outcome.changes if c.change_type is ChangeType.REMOVED_PRODUCT]
    assert [(c.bank, c.rate_category) for c in removed] == [("ASB", "special")]
    assert market.asb12(market.when(1, 1)) == (4.00, "term_deposit")


def test_category_level_coverage_regression_is_degraded(tmp_path):
    market = Market(tmp_path, fixture("d_special_column_heading.html").replace(
        "Special rate</th>", "Special rate</th><th>Availability</th>").replace(
        "3.85% p.a.</td>", "3.85% p.a.</td><td>Available to all customers</td>").replace(
        "4.40% p.a.</td>", "4.40% p.a.</td><td>Available to all customers</td>"))  # fmt: skip
    assert market.run(0).code == EXIT_OK
    # The special column disappears from the parse while the standard rows remain.
    market.set_asb(fixture("d_special_column_heading.html").replace(
        "<th>Special rate</th>", "").replace("<td>3.85% p.a.</td>", "").replace(
        "<td>4.40% p.a.</td>", ""))  # fmt: skip
    result = market.run(1)
    assert result.code == EXIT_PARTIAL_FAILURE
    failures = summary_of(result)["failures"]
    assert any(f["bank"] == "ASB" and f["error_type"] == "category_low_coverage" for f in failures)
    assert not [c for c in result.outcome.changes if c.change_type is ChangeType.REMOVED_PRODUCT]


# --------------------------------------------------------- comparability (T09, T10, T14)


def test_t09_pie_returns_stay_out_of_the_ordinary_td_benchmark(tmp_path):
    market = Market(tmp_path, fixture("l_pie_and_ordinary.html"))
    assert market.run(0).code == EXIT_OK
    assert market.asb12(market.when(0, 1)) == (4.00, "term_deposit")
    assert market.term12(market.when(0, 1)).best_rate == 4.05


def test_t10_multiple_balance_tiers_remain_distinct():
    records = [r for r in parse_asb(fixture("g_multiple_balance_tiers.html")) if r.term_months_normalised == 12]
    tiers = sorted((r.min_balance_or_deposit, r.max_balance_or_deposit, r.advertised_rate_pct) for r in records)
    assert tiers == [(10_000.0, 499_999.0, 4.00), (500_000.0, 5_000_000.0, 4.25)]
    assert len({r.observation_key() for r in records}) == 2


def test_t14_monthly_and_at_maturity_frequencies_remain_distinguishable():
    records = parse_asb(fixture("jk_payment_frequencies.html"))
    assert sorted((r.interest_payment_frequency, r.advertised_rate_pct) for r in records) == [
        ("at_maturity", 4.00), ("monthly", 3.90)
    ]
    assert len({r.observation_key() for r in records}) == 2


# ------------------------------------------------------- balance normalisation (T11-T13)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$500k+", (500_000.0, None)),
        ("$1m+", (1_000_000.0, None)),
        ("$1M+", (1_000_000.0, None)),
        ("$10K+", (10_000.0, None)),
        ("$2.5m+", (2_500_000.0, None)),
        ("$5,000+", (5_000.0, None)),
    ],
)
def test_t11_abbreviated_amounts_normalise(text, expected):
    assert normalise_balance(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$10k to $49,999", (10_000.0, 49_999.0)),
        ("$10,000 - $499,999", (10_000.0, 499_999.0)),
        ("$5k - $499,999", (5_000.0, 499_999.0)),
        ("$500k-$1m", (500_000.0, 1_000_000.0)),
    ],
)
def test_t12_explicit_ranges_normalise(text, expected):
    assert normalise_balance(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Minimum $1,000 | Balance $5,000 to $9,999", (1_000.0, None)),
        ("Minimum $1k | Minimum $5k", (1_000.0, None)),
        ("$1,000 minimum then $5,000", (1_000.0, None)),
        ("Minimum $1,000 per month", (1_000.0, None)),  # "m" of a word is not a multiplier
    ],
)
def test_t13_unrelated_minimums_never_invent_a_maximum(text, expected):
    assert normalise_balance(text) == expected


def test_abbreviated_tiers_parse_from_a_page_with_stable_identity():
    records = parse_asb(fixture("h_abbreviated_balance_tiers.html"))
    assert sorted((r.min_balance_or_deposit, r.max_balance_or_deposit) for r in records) == [
        (5_000.0, 499_999.0), (500_000.0, None), (1_000_000.0, None)
    ]
    again = parse_asb(fixture("h_abbreviated_balance_tiers.html"))
    assert {r.observation_key() for r in records} == {r.observation_key() for r in again}
    (row,) = parse_asb(fixture("i_balance_range_k_notation.html"))
    assert (row.min_balance_or_deposit, row.max_balance_or_deposit) == (10_000.0, 49_999.0)


# ------------------------------------------------------------ ranking (T15-T17)


def test_t15_same_bank_counts_once_per_term_with_a_comparable_special(tmp_path):
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    market.run(0)
    term = market.term12(market.when(0, 1))
    assert [e.bank for e in term.entries].count("ASB") == 1
    assert term.bank_count == len(BANKS)


def test_t16_deposit_leader_is_highest_eligible_and_ties_prefer_the_ordinary_rate():
    standard = _td(rate=4.45)
    special = _td(rate=4.45, category="special",
                  context="12 Months | 4.45% | Available to all customers")  # fmt: skip
    other = _td(bank="BNZ", rate=4.05)
    for records in ([standard, special, other], [special, standard, other]):
        snap = build_market_snapshot(
            records, product_type=TD, rate_category="term_deposit",
            admitted_keys={special.observation_key()},
        )  # fmt: skip
        assert snap.term(12).market_leaders == ("ASB",)
        assert snap.term(12).entries[0].rate_category == "term_deposit"


def test_t17_loan_leader_still_selects_the_lowest_rate(make_record):
    snap = build_market_snapshot(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="ASB", rate_pct=4.69)],
        product_type=ProductType.HOME_LOAN,
    )
    assert snap.term(12).market_leaders == ("ASB",) and snap.term(12).best_rate == 4.69


# ------------------------------------------------------- idempotency and history (T18, T23)


def test_t18_repeated_collection_of_unchanged_data_is_idempotent(tmp_path):
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    first = market.run(0)
    second = market.run(1)
    assert (first.code, second.code) == (EXIT_OK, EXIT_OK)
    assert second.outcome.changes == () and second.outcome.write_result.inserted == 0
    assert specials_in(first) == specials_in(second)
    replay = market.run(1)  # the same run_id again
    assert replay.outcome.already_recorded and replay.code == EXIT_OK


def test_t23_previously_stored_td_records_remain_readable_and_assessable(tmp_path):
    store = ObservationStore(str(tmp_path / "obs.db"))
    old = [_td(), _td(rate=4.5, category="special", context="12 Months Special | 4.50% p.a.")]
    store.record_observations(old, run_id="legacy", observed_at=T0, product_type=TD)
    reread = store.get_current_observations(product_type=TD)
    assert sorted(r.rate_category for r in reread) == ["special", "term_deposit"]
    (assessment,) = assess_deposit_specials(
        reread, restriction_markers=(), availability_markers=("available to all",),
        balance_tier_edges=(10_000.0,),
    )  # fmt: skip
    assert assessment.status is SpecialComparability.UNCERTAIN


# ------------------------------------------------------------ Phase 2a review (T21, T22)


def _quarantined_special(tmp_path) -> tuple[Market, object]:
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    market.run(0)
    market.set_asb(fixture("c_special_separate_table.html").replace("4.45% p.a.", "5.45% p.a."))
    market.run(1)
    (case,) = market.service.list_review_cases(TD, now=market.when(1, 1))
    assert case.bank == "ASB" and case.proposed_rate_pct == 5.45
    assert market.asb12(market.when(1, 1)) == (4.45, "special")  # last trusted state
    return market, case


def test_t21_rejected_special_stays_out_of_the_benchmark(tmp_path):
    market, case = _quarantined_special(tmp_path)
    market.service.review(case.event_id, ReviewAction.REJECT, reviewer="alice",
                          reason="misread", reviewed_at=market.when(1, 2))  # fmt: skip
    market.run(2)
    assert market.term12(market.when(2, 1)).best_rate != 5.45


def test_t21_voided_special_falls_back_to_its_trusted_predecessor(tmp_path):
    market = Market(tmp_path, fixture("c_special_separate_table.html"))
    market.run(0)
    market.set_asb(fixture("c_special_separate_table.html").replace("4.45%", "4.50%"))
    market.run(1)
    assert market.term12(market.when(1, 1)).best_rate == 4.50  # accepted +5 bps move
    (accepted,) = [
        c for c in market.service.list_review_cases(TD, [ReviewState.ACCEPTED], now=market.when(1, 1))
        if c.bank == "ASB" and c.proposed_rate_pct == 4.50
    ]
    market.service.review(accepted.event_id, ReviewAction.VOID, reviewer="bob",
                          reason="source parsing error", reviewed_at=market.when(1, 2))  # fmt: skip
    assert market.term12(market.when(1, 3)).best_rate == 4.45
    assert market.asb12(market.when(1, 3)) == (4.45, "special")


def test_t22_approved_special_becomes_trusted_and_comparable(tmp_path):
    market, case = _quarantined_special(tmp_path)
    market.service.review(case.event_id, ReviewAction.APPROVE, reviewer="alice",
                          reason="confirmed on the rate card", evidence_ref="evidence/asb.png",
                          reviewed_at=market.when(1, 2))  # fmt: skip
    assert market.asb12(market.when(1, 3)) == (5.45, "special")
    assert market.term12(market.when(1, 3)).best_rate == 5.45

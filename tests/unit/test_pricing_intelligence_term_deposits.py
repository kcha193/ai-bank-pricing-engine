"""Term-deposit collection, tested against the real page quirks.

Every fixture here reproduces a structure observed during live verification on
2026-08-27. Deposit pages are messier than mortgage pages: banks publish
managed-fund returns and tax-adjusted PIE "effective rates" in the same tables
as advertised deposit rates, and deposits rank highest-is-best, so anything
inflated wins the market-leader position.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    ANZCollector,
    BNZCollector,
    KiwibankCollector,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.competitor_rates.normalise import normalise_balance
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot

FIXTURES = Path("tests/fixtures/pricing_intelligence")
OBSERVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)


def collect(collector_class, fixture_name):
    html = (FIXTURES / fixture_name).read_bytes()
    collector = collector_class(fetcher=lambda url: html)
    return collector.collect_term_deposit_rates(
        captured_at_utc=OBSERVED_AT, persist_snapshot=False
    )


# ----------------------------------------------------- balance range parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The Kiwibank case: a row minimum plus a heading minimum, not a range.
        ("30 days Minimum balance $5,000 | Term Deposit Minimum balance $1,000", (5000.0, None)),
        ("$10,000 to $49,999", (10000.0, 49999.0)),
        ("$5,000+", (5000.0, None)),
        ("no amounts", (None, None)),
    ],
)
def test_two_unrelated_minimums_do_not_become_a_range(text, expected):
    assert normalise_balance(text) == expected


def test_a_descending_pair_never_produces_an_invalid_record():
    """max < min is rejected by the model, which previously killed the parse."""
    minimum, maximum = normalise_balance("Minimum $5,000 ... Minimum balance $1,000")

    assert maximum is None or maximum >= minimum


# ------------------------------------------------------ per-row isolation


def test_one_unparseable_row_does_not_cost_the_whole_bank():
    """Kiwibank returned 0 of its rates because a single row was invalid."""
    result = collect(KiwibankCollector, "kiwibank_term_deposits.html")

    assert result.succeeded
    assert result.rate_count >= 2, "the valid rows must survive"
    rates = {r.advertised_rate_pct for r in result.records}
    assert 4.00 in rates
    assert 3.50 in rates


# ------------------------------------------------- non-comparable products


def test_managed_fund_returns_are_not_term_deposits():
    """BNZ lists Growth/Balanced fund returns on its investments page."""
    records = collect(BNZCollector, "bnz_term_deposits.html").records
    comparable = [r for r in records if r.rate_category == "term_deposit"]

    assert all(r.advertised_rate_pct < 6.0 for r in comparable), (
        "a 12.47% fund return must not be treated as a term-deposit rate"
    )
    assert any(r.rate_category == "non_comparable" for r in records)


def test_pie_effective_rates_are_not_advertised_rates():
    """ANZ publishes tax-adjusted effective returns beside the advertised rate.

    Deposits rank highest-is-best, so including these made ANZ and Westpac look
    like market leaders by 70 bps when their advertised rate is at parity.
    """
    records = collect(ANZCollector, "anz_term_deposits.html").records
    one_year = [
        r
        for r in records
        if r.term_months_normalised == 12 and r.rate_category == "term_deposit"
    ]

    assert {r.advertised_rate_pct for r in one_year} == {3.90}
    excluded = {
        r.advertised_rate_pct for r in records if r.rate_category == "non_comparable"
    }
    assert {4.01, 4.19, 4.60} <= excluded


def test_pir_abbreviated_effective_rate_rows_are_excluded_from_comparison(make_record):
    """Banks commonly abbreviate "prescribed investor rate" to "PIR" on the
    page itself (e.g. "PIR 28%: 4.01%") rather than spelling it out. Without
    recognising the abbreviation, this row keeps the generic term_deposit
    category and can outrank every genuinely advertised rate for
    MARKET_LEADER."""
    record = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="term_deposit",
        rate_pct=4.01,
        raw_text_context="1 year PIR 28%: 4.01%",
    )

    enriched = ANZCollector().enrich_term_deposit_record(record)

    assert enriched.rate_category == "non_comparable"


def test_pir_marker_does_not_match_inside_an_unrelated_word(make_record):
    """A legitimate row mentioning e.g. "expires" must not be excluded just
    because "pir" is a substring of that word -- the abbreviation must be
    matched as a standalone token, not a plain substring."""
    record = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="term_deposit",
        rate_pct=4.50,
        raw_text_context="1 year special rate expires 30 September 4.50%",
    )

    enriched = ANZCollector().enrich_term_deposit_record(record)

    assert enriched.rate_category == "term_deposit"


def test_excluded_deposit_products_are_retained_for_audit():
    records = collect(ANZCollector, "anz_term_deposits.html").records
    excluded = [r for r in records if r.rate_category == "non_comparable"]

    assert excluded
    for record in excluded:
        assert record.source_url
        assert record.raw_text_context, "provenance must survive exclusion"


# --------------------------------------------------------- market snapshot


def test_deposit_snapshot_ranks_highest_rate_first():
    records = []
    for collector, fixture in (
        (ANZCollector, "anz_term_deposits.html"),
        (BNZCollector, "bnz_term_deposits.html"),
        (KiwibankCollector, "kiwibank_term_deposits.html"),
    ):
        records += [
            r
            for r in collect(collector, fixture).records
            if r.rate_category == "term_deposit"
        ]

    snapshot = build_market_snapshot(
        records,
        product_type=ProductType.TERM_DEPOSIT,
        rate_category="term_deposit",
        terms_months=[6, 12, 24],
    )
    market = snapshot[12]

    assert market.best_rate == 4.05, "BNZ leads at 1 year"
    assert market.market_leader == "BNZ"
    assert market.ranking()[0].bank == "BNZ"
    assert market.rate_for("ANZ") == 3.90


def test_deposit_snapshot_excludes_non_comparable_products():
    records = collect(BNZCollector, "bnz_term_deposits.html").records
    snapshot = build_market_snapshot(
        records,
        product_type=ProductType.TERM_DEPOSIT,
        rate_category="term_deposit",
        terms_months=[6, 12, 24],
    )

    for term_market in snapshot.terms.values():
        assert all(entry.rate_pct < 6.0 for entry in term_market.entries)


# ------------------------------------------------- pricing-engine integration


def _deposit_snapshot():
    records = []
    for collector, fixture in (
        (ANZCollector, "anz_term_deposits.html"),
        (BNZCollector, "bnz_term_deposits.html"),
        (KiwibankCollector, "kiwibank_term_deposits.html"),
    ):
        records += [
            r
            for r in collect(collector, fixture).records
            if r.rate_category == "term_deposit"
        ]
    return build_market_snapshot(
        records,
        product_type=ProductType.TERM_DEPOSIT,
        rate_category="term_deposit",
        terms_months=[6, 12, 24],
    )


@pytest.fixture
def engine_config():
    from balance_pricing_engine.config import load_config

    return load_config("configs/base.yaml")


def _mortgage_snapshot():
    from balance_pricing_engine.competitor_rates.collectors import ASBCollector

    html = (FIXTURES / "asb_home_loans_unmarked.html").read_bytes()
    records = (
        ASBCollector(fetcher=lambda url: html)
        .collect_mortgage_rates(captured_at_utc=OBSERVED_AT, persist_snapshot=False)
        .records
    )
    return build_market_snapshot(records, rate_category="special")


def test_both_sides_can_be_overlaid_from_their_own_snapshots(engine_config):
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        apply_market_strategy_to_config,
    )

    result = apply_market_strategy_to_config(
        engine_config,
        strategy=CompetitorRateStrategy.MARKET_LEADER,
        snapshot=_mortgage_snapshot(),
        term_months=12,
        deposit_snapshot=_deposit_snapshot(),
        deposit_strategy=CompetitorRateStrategy.MARKET_LEADER,
        deposit_term_months=12,
    )

    assert result.market.competitor_deposit_rate == 0.0405, "BNZ leads at 4.05%"
    assert result.market.competitor_loan_rate == 0.0499, "ASB 12-month special is 4.99%"
    assert engine_config.market.competitor_deposit_rate != 0.0405


def test_a_market_loan_strategy_without_a_loan_snapshot_fails_loudly(engine_config):
    """Silently ignoring a requested market strategy would be worse than raising."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        MarketStrategyError,
        apply_market_strategy_to_config,
    )

    with pytest.raises(MarketStrategyError, match="requires a market snapshot"):
        apply_market_strategy_to_config(
            engine_config,
            strategy=CompetitorRateStrategy.MARKET_LEADER,
            deposit_snapshot=_deposit_snapshot(),
            deposit_strategy=CompetitorRateStrategy.MARKET_LEADER,
        )


def test_a_market_deposit_strategy_without_a_deposit_snapshot_fails_loudly(engine_config):
    """Silently ignoring a requested deposit market strategy would be worse
    than raising -- the same invariant the loan side already enforces."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        MarketStrategyError,
        apply_market_strategy_to_config,
    )

    with pytest.raises(MarketStrategyError, match="requires a market snapshot"):
        apply_market_strategy_to_config(
            engine_config,
            strategy=CompetitorRateStrategy.MANUAL,
            deposit_strategy=CompetitorRateStrategy.MARKET_MEDIAN,
        )


def test_manual_strategy_ignores_an_irrelevant_mismatched_snapshot(engine_config):
    """MANUAL never consults its snapshot (resolve_competitor_rate() short-
    circuits before touching it), so an irrelevant -- even wrong-product --
    snapshot passed alongside it must not raise: the documented "MANUAL
    returns the config unchanged" behaviour must hold regardless of what
    snapshot happens to be in scope."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        apply_market_strategy_to_config,
    )

    result = apply_market_strategy_to_config(
        engine_config,
        strategy=CompetitorRateStrategy.MANUAL,
        snapshot=_deposit_snapshot(),
    )

    assert result is engine_config


def test_manual_loan_strategy_does_not_block_a_valid_deposit_overlay(engine_config):
    """A wrong-typed loan-side snapshot passed alongside MANUAL must not
    block an otherwise-valid deposit-side market overlay -- validation is
    conditioned on the side actually using a market-derived strategy."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        apply_market_strategy_to_config,
    )

    result = apply_market_strategy_to_config(
        engine_config,
        strategy=CompetitorRateStrategy.MANUAL,
        snapshot=_deposit_snapshot(),
        deposit_snapshot=_deposit_snapshot(),
        deposit_strategy=CompetitorRateStrategy.MARKET_LEADER,
    )

    assert result.market.competitor_deposit_rate == 0.0405, "BNZ leads at 4.05%"
    assert result.market.competitor_loan_rate == engine_config.market.competitor_loan_rate


def test_a_home_loan_snapshot_cannot_be_used_as_the_deposit_overlay(engine_config):
    """A home-loan snapshot passed as deposit_snapshot must be rejected, not
    silently written into competitor_deposit_rate -- ranking direction comes
    from the snapshot too, so MARKET_LEADER would otherwise resolve the
    mortgage market's benchmark instead of the deposit market's."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        MarketStrategyError,
        apply_market_strategy_to_config,
    )

    with pytest.raises(MarketStrategyError, match="deposit-side snapshot must be a term_deposit"):
        apply_market_strategy_to_config(
            engine_config,
            strategy=CompetitorRateStrategy.MANUAL,
            deposit_snapshot=_mortgage_snapshot(),
            deposit_strategy=CompetitorRateStrategy.MARKET_LEADER,
        )


def test_a_deposit_snapshot_cannot_be_used_as_the_loan_overlay(engine_config):
    """The symmetric case: a term-deposit snapshot passed as the loan-side
    ``snapshot`` must be rejected too."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        MarketStrategyError,
        apply_market_strategy_to_config,
    )

    with pytest.raises(MarketStrategyError, match="loan-side snapshot must be a home_loan"):
        apply_market_strategy_to_config(
            engine_config,
            strategy=CompetitorRateStrategy.MARKET_LEADER,
            snapshot=_deposit_snapshot(),
        )


def test_omitting_a_deposit_snapshot_leaves_the_deposit_side_alone(engine_config):
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        apply_market_strategy_to_config,
    )

    result = apply_market_strategy_to_config(
        engine_config,
        strategy=CompetitorRateStrategy.MARKET_MEDIAN,
        snapshot=_mortgage_snapshot(),
        term_months=12,
    )

    assert (
        result.market.competitor_deposit_rate
        == engine_config.market.competitor_deposit_rate
    )


def test_manual_on_both_sides_still_returns_the_identical_config(engine_config):
    """The invariant that protects every pre-existing scenario."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        apply_market_strategy_to_config,
    )

    result = apply_market_strategy_to_config(
        engine_config, strategy=CompetitorRateStrategy.MANUAL
    )

    assert result is engine_config


def test_each_side_resolves_independently(engine_config):
    """A market loan rate with a manual deposit assumption is legitimate."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        CompetitorRateStrategy,
        apply_market_strategy_to_config,
    )

    result = apply_market_strategy_to_config(
        engine_config,
        strategy=CompetitorRateStrategy.MANUAL,
        deposit_snapshot=_deposit_snapshot(),
        deposit_strategy=CompetitorRateStrategy.MARKET_LEADER,
        deposit_term_months=12,
    )

    assert result.market.competitor_loan_rate == engine_config.market.competitor_loan_rate
    assert result.market.competitor_deposit_rate == 0.0405


def test_non_comparable_rows_do_not_flood_validation_with_conflicts(make_record):
    """Excluded rows would otherwise collide on observation_key(); the guard
    must not fire on genuinely different products just because the category
    lumps them together.

    PIE effective-rate columns are deliberately lumped into one category, so
    rate_category/term/bank alone is not a meaningful identity for them --
    each live row still carries its own fund name, the structural text
    _disambiguate_non_comparable_identity() now keys off. Applying the
    conflict guard without that disambiguation rejected 121 of 224 live
    deposit records and would drown a scheduled run's logs.
    """
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    excluded = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            product_name=f"PIE Term Fund {label}",
            rate_pct=rate,
            raw_text_context=f"PIE Term Fund {label} effective rate {rate}%",
        )
        for label, rate in (("A", 4.01), ("B", 4.19), ("C", 4.60))
    ]

    outcome = validate_observations(excluded)

    assert outcome.rejected == []
    assert not [i for i in outcome.errors if i.code == "conflicting_observation"]


def test_non_comparable_rows_get_a_stable_content_derived_identity(make_record):
    """Two non-comparable rows with different raw_text_context (representing
    two genuinely different, if unlabelled, products) must not share an
    observation_key() -- collapsing them onto one identity is what corrupted
    ObservationStore.record_observations() into writing every one of them as
    a fresh state on every re-collection, even when nothing on the source
    page had changed."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    excluded = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=4.01,
            raw_text_context="PIE Term Fund A effective rate 4.01%",
        ),
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=4.01,
            raw_text_context="PIE Term Fund B effective rate 4.01%",
        ),
    ]

    outcome = validate_observations(excluded)

    assert len(outcome.accepted) == 2
    assert outcome.accepted[0].observation_key() != outcome.accepted[1].observation_key()


def test_mortgage_non_comparable_identities_are_left_untouched(make_record):
    """Disambiguation is scoped to deposits: a mortgage's non_comparable
    products (subsidised/green top-ups, closed products) are already
    individually named by the extractor, so touching product_name here would
    break pairing against every already-stored mortgage identity."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    record = make_record(
        bank="ASB",
        product_type="home_loan",
        rate_category="non_comparable",
        product_name="Back My Build",
    )

    outcome = validate_observations([record])

    assert outcome.accepted[0].product_name == "Back My Build"


def test_non_comparable_rows_differing_only_by_a_structural_percentage_stay_distinct(
    make_record,
):
    """A PIR/tax-band qualifier (e.g. "PIR 28%" vs "PIR 33%") is itself part
    of what distinguishes two otherwise-similar deposit rows -- stripping
    every percentage rather than only the advertised rate would collapse
    them onto the same identity and reject them as a conflict instead of
    keeping both as distinct audit records."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    excluded = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=4.01,
            raw_text_context="PIR 28%: 4.01%",
        ),
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=4.19,
            raw_text_context="PIR 33%: 4.19%",
        ),
    ]

    outcome = validate_observations(excluded)

    assert outcome.rejected == []
    assert len(outcome.accepted) == 2
    assert outcome.accepted[0].observation_key() != outcome.accepted[1].observation_key()


def test_non_comparable_identity_is_stable_across_a_genuine_rate_change(make_record):
    """raw_text_context contains the parsed percentage, so a genuine rate
    update on the same underlying row must not change its identity -- that
    would make change detection read it as that product being withdrawn and
    a new one appearing, rather than a rate update on the same key."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    before = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=4.01,
        raw_text_context="PIE Term Fund A effective rate 4.01%",
    )
    after = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=4.25,
        raw_text_context="PIE Term Fund A effective rate 4.25%",
    )

    before_key = validate_observations([before]).accepted[0].observation_key()
    after_key = validate_observations([after]).accepted[0].observation_key()

    assert before_key == after_key


def test_non_comparable_identity_is_stable_across_a_mutable_sibling_figure(make_record):
    """A non_comparable row can carry a second, independently-mutating
    percentage besides its own advertised rate -- BNZ's real fund-return rows
    (see bnz_term_deposits.html) carry both a "Return" (the parsed rate) and
    a "Since inception" return in the same flattened raw_text_context:
    "1 year | 12.47% | 15.28% | Growth Fund | Return". That sibling figure
    drifting between collections, with the row's own rate unchanged, must
    not change the row's identity -- otherwise every drift reads as the
    product being withdrawn and a new one appearing.

    Exercised the way production actually calls validate_observations(): the
    second collection's ``previous`` is the first collection's own accepted
    (already-tagged) output, since identity now comes from continuity with
    history, not from recomputing fresh from this run's raw text alone."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    before = make_record(
        bank="BNZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=12.47,
        raw_text_context="1 year | 12.47% | 15.28% | Growth Fund | Return",
    )
    tagged_before = validate_observations([before]).accepted[0]

    after = make_record(
        bank="BNZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=12.47,
        raw_text_context="1 year | 12.47% | 16.02% | Growth Fund | Return",
    )
    tagged_after = validate_observations([after], previous=[tagged_before]).accepted[0]

    assert tagged_before.observation_key() == tagged_after.observation_key()


def test_non_comparable_identity_survives_a_colliding_sibling_disappearing(make_record):
    """Two rows told apart only by a PIR/tax-band percentage must keep their
    own separately-established identity even after the other one stops
    being collected.

    A batch-relative disambiguation rule (an earlier design) would flip the
    survivor onto a different identity the moment it stops colliding with
    its sibling, reading a completely unchanged row as withdrawn and a new
    one appearing. Continuity is anchored in ``previous`` instead, which
    still holds both rows even though only one is collected again."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    pir_28 = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=4.01,
        raw_text_context="PIR 28%: 4.01%",
    )
    pir_33 = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=4.19,
        raw_text_context="PIR 33%: 4.19%",
    )
    first_run = validate_observations([pir_28, pir_33]).accepted
    tagged_28 = next(r for r in first_run if "28" in r.raw_text_context)

    pir_28_again = make_record(
        bank="ANZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=4.01,
        raw_text_context="PIR 28%: 4.01%",
    )
    second_run = validate_observations([pir_28_again], previous=first_run).accepted

    assert len(second_run) == 1
    assert second_run[0].observation_key() == tagged_28.observation_key()


def test_non_comparable_rows_stay_distinct_despite_a_shared_sibling_figure(make_record):
    """Growth Fund and Balanced Fund publish the exact same "Since inception"
    figure (15.28%, see bnz_term_deposits.html) but are genuinely different
    products at different rates -- the fund name, not a shared percentage,
    is what must keep them distinct."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    growth = make_record(
        bank="BNZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=12.47,
        raw_text_context="1 year | 12.47% | 15.28% | Growth Fund | Return",
    )
    balanced = make_record(
        bank="BNZ",
        product_type="term_deposit",
        rate_category="non_comparable",
        rate_pct=9.76,
        raw_text_context="1 year | 9.76% | 15.28% | Balanced Fund | Return",
    )

    outcome = validate_observations([growth, balanced])

    assert outcome.rejected == []
    assert len(outcome.accepted) == 2
    assert outcome.accepted[0].observation_key() != outcome.accepted[1].observation_key()


def test_a_repeated_collection_of_non_comparable_rows_reports_unchanged(make_record):
    """The actual corruption the disambiguation guards against: previously,
    re-collecting an unchanged page re-inserted every colliding
    non_comparable row as a fresh state instead of recognising it as
    unchanged, growing the database on every scheduled run."""
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    store = ObservationStore(":memory:")
    excluded = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=rate,
            raw_text_context=f"PIE Term Fund {label} effective rate {rate}%",
        )
        for label, rate in (("A", 4.01), ("B", 4.19), ("C", 4.60))
    ]

    first = store.record_observations(
        validate_observations(excluded).accepted, observed_at=OBSERVED_AT
    )
    assert first.inserted == 3

    second = store.record_observations(
        validate_observations(excluded).accepted,
        observed_at=OBSERVED_AT + timedelta(days=1),
    )

    assert second.inserted == 0
    assert second.unchanged == 3


def test_identical_raw_text_with_different_rates_still_triggers_the_conflict_guard(
    make_record,
):
    """A genuine parser fault -- the same row text somehow producing two
    different rates -- must still be rejected loudly, not silently exempted
    just because the category happens to be non_comparable."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    excluded = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=rate,
            raw_text_context="PIE Term Fund A effective rate",
        )
        for rate in (4.01, 4.19)
    ]

    outcome = validate_observations(excluded)

    assert len(outcome.rejected) == 2
    assert [i for i in outcome.errors if i.code == "conflicting_observation"]


def test_the_conflict_guard_still_fires_on_comparable_rows(make_record):
    """Exempting non-comparable rows must not weaken the real protection."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    conflicting = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="term_deposit",
            rate_pct=rate,
        )
        for rate in (3.90, 4.05)
    ]

    outcome = validate_observations(conflicting)

    assert [i.code for i in outcome.errors] == ["conflicting_observation"] * 2
    assert len(outcome.rejected) == 2

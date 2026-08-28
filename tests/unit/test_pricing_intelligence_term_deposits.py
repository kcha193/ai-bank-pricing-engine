"""Term-deposit collection, tested against the real page quirks.

Every fixture here reproduces a structure observed during live verification on
2026-08-27. Deposit pages are messier than mortgage pages: banks publish
managed-fund returns and tax-adjusted PIE "effective rates" in the same tables
as advertised deposit rates, and deposits rank highest-is-best, so anything
inflated wins the market-leader position.
"""

from datetime import datetime, timezone
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
    """Excluded rows collide by construction; the guard must not fire on them.

    PIE effective-rate columns are deliberately lumped into one category, so
    their identity is not meaningful. Applying the conflict guard there rejected
    121 of 224 live deposit records and would drown a scheduled run's logs.
    """
    from balance_pricing_engine.pricing_intelligence.validation import (
        validate_observations,
    )

    excluded = [
        make_record(
            bank="ANZ",
            product_type="term_deposit",
            rate_category="non_comparable",
            rate_pct=rate,
        )
        for rate in (4.01, 4.19, 4.60)
    ]

    outcome = validate_observations(excluded)

    assert outcome.rejected == []
    assert not [i for i in outcome.errors if i.code == "conflicting_observation"]


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

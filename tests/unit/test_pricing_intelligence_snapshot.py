"""Market statistics and ranking, including ties."""

from datetime import datetime, timezone

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot

OBSERVED_AT = datetime(2026, 8, 20, tzinfo=timezone.utc)

# The worked example from the specification.
MARKET = [
    ("ANZ", 4.79),
    ("BNZ", 4.85),
    ("Westpac", 4.85),
    ("ASB", 4.89),
    ("Kiwibank", 4.89),
]


@pytest.fixture
def snapshot(make_record):
    records = [
        make_record(bank=bank, term_months=12, rate_pct=rate) for bank, rate in MARKET
    ]
    return build_market_snapshot(records)


def test_market_statistics(snapshot):
    market = snapshot[12]

    assert market.lowest_rate == 4.79
    assert market.highest_rate == 4.89
    assert market.median_rate == 4.85
    assert market.mean_rate == pytest.approx(4.854)
    assert market.best_rate == 4.79
    assert market.bank_count == 5


def test_spread_is_exact_in_basis_points(snapshot):
    assert snapshot[12].spread_bps == 10.0


def test_ranking_handles_ties_with_standard_competition_ranking(snapshot):
    ranks = {entry.bank: entry.rank for entry in snapshot[12].ranking()}

    assert ranks == {"ANZ": 1, "BNZ": 2, "Westpac": 2, "ASB": 4, "Kiwibank": 4}


def test_excluding_bank_matches_regardless_of_case_or_whitespace(snapshot):
    """our_bank is an arbitrary config string, not validated against the
    collectors' canonical names -- "asb" or " ASB " must exclude the same
    bank as "ASB", or a config typo would leave the tracked bank's own
    advertised rate counted in every market statistic it should be excluded
    from."""
    excluded = snapshot[12].excluding_bank(" asb ")

    assert "ASB" not in {entry.bank for entry in excluded.entries}
    assert excluded.bank_count == 4


def test_ranking_is_ordered_most_competitive_first(snapshot):
    ordered = [entry.bank for entry in snapshot[12].ranking()]

    assert ordered[0] == "ANZ"
    assert ordered[1:3] == ["BNZ", "Westpac"]


def test_market_leader_and_leaders(snapshot):
    assert snapshot[12].market_leader == "ANZ"
    assert snapshot[12].market_leaders == ("ANZ",)


def test_tied_leaders_are_all_reported(make_record):
    records = [
        make_record(bank="ANZ", rate_pct=4.79),
        make_record(bank="BNZ", rate_pct=4.79),
        make_record(bank="ASB", rate_pct=4.99),
    ]
    market = build_market_snapshot(records)[12]

    assert set(market.market_leaders) == {"ANZ", "BNZ"}
    assert market.market_leader == "ANZ", "single leader is deterministic (alphabetical)"
    assert [entry.rank for entry in market.ranking()] == [1, 1, 3]


def test_second_best_excludes_every_tied_leader(make_record):
    records = [
        make_record(bank="ANZ", rate_pct=4.79),
        make_record(bank="BNZ", rate_pct=4.79),
        make_record(bank="ASB", rate_pct=4.99),
    ]

    assert build_market_snapshot(records)[12].second_best_rate == 4.99


def test_second_best_is_none_when_all_banks_are_tied(make_record):
    records = [make_record(bank=bank, rate_pct=4.79) for bank in ("ANZ", "BNZ")]

    assert build_market_snapshot(records)[12].second_best_rate is None


def test_a_bank_advertising_two_rates_at_one_term_appears_once(make_record):
    records = [
        make_record(bank="ANZ", rate_pct=4.79),
        make_record(bank="ANZ", rate_pct=4.95, term_label_raw="1 year fixed"),
        make_record(bank="BNZ", rate_pct=4.85),
    ]
    market = build_market_snapshot(records)[12]

    assert market.bank_count == 2
    assert market.rate_for("ANZ") == 4.79, "the most competitive rate represents the bank"


def test_a_non_comparable_product_does_not_win_market_leader(make_record):
    """ASB's construction-only "Back My Build" must not out-compete standard
    fixed lending on rate alone -- see ASBCollector's docstring."""
    records = [
        make_record(bank="ASB", rate_pct=4.29, product_name="Back My Build"),
        make_record(bank="ASB", rate_pct=4.89, product_name="Fixed Special"),
        make_record(bank="BNZ", rate_pct=4.85),
    ]
    market = build_market_snapshot(records, excluded_product_names=("Back My Build",))[12]

    assert market.rate_for("ASB") == 4.89
    assert market.market_leader == "BNZ"


def test_excluded_product_name_matches_case_insensitively(make_record):
    records = [make_record(bank="ASB", rate_pct=4.29, product_name="back my build special")]

    snapshot = build_market_snapshot(records, excluded_product_names=("Back My Build",))

    assert 12 not in snapshot


def test_rate_category_filter_compares_like_with_like(make_record):
    records = [
        make_record(bank="ANZ", rate_pct=4.79, rate_category="special"),
        make_record(bank="ANZ", rate_pct=5.39, rate_category="standard"),
        make_record(bank="BNZ", rate_pct=4.85, rate_category="special"),
    ]

    specials = build_market_snapshot(records, rate_category="special")[12]
    standards = build_market_snapshot(records, rate_category="standard")[12]

    assert specials.bank_count == 2
    assert standards.bank_count == 1
    assert standards.best_rate == 5.39


def test_deposit_direction_treats_the_highest_rate_as_best(make_record):
    records = [
        make_record(bank="ANZ", rate_pct=4.10, product_type="term_deposit"),
        make_record(bank="BNZ", rate_pct=4.40, product_type="term_deposit"),
        make_record(bank="ASB", rate_pct=4.25, product_type="term_deposit"),
    ]
    market = build_market_snapshot(
        records, product_type=ProductType.TERM_DEPOSIT, rate_category="special"
    )[12]

    assert market.best_rate == 4.40
    assert market.market_leader == "BNZ"
    assert market.rank_for("BNZ") == 1
    assert market.rank_for("ANZ") == 3


def test_snapshot_covers_multiple_terms(make_record):
    records = [
        make_record(bank="ANZ", term_months=12, rate_pct=4.79),
        make_record(bank="ANZ", term_months=24, rate_pct=4.95),
        make_record(bank="BNZ", term_months=24, rate_pct=4.99),
    ]
    snapshot = build_market_snapshot(records)

    assert snapshot.term_months == (12, 24)
    assert 12 in snapshot
    assert snapshot.term(36) is None


def test_records_without_a_normalised_term_are_excluded(make_record):
    records = [
        make_record(bank="ANZ", rate_pct=4.79),
        make_record(
            bank="ANZ", rate_pct=6.49, term_months_normalised=None, term_label_raw="Floating"
        ),
    ]
    snapshot = build_market_snapshot(records)

    assert snapshot.term_months == (12,)


def test_render_produces_a_bank_by_term_grid(snapshot):
    rendered = snapshot.render()

    assert "Bank" in rendered
    assert "ANZ" in rendered
    assert "Median" in rendered
    assert "Spread bps" in rendered

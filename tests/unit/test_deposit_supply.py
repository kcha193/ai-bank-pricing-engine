"""Independent checks of Phase 4c opportunity, stock/flow and funding attribution."""

import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.deposit_supply import (
    ConcentrationMode,
    DepositConcentrationPolicy,
    DepositFlow,
    DepositMarketProfile,
    DepositOpportunitySegment,
    DepositSupply,
)
from balance_pricing_engine.pricing.profit import (
    expected_excess_deposit_income,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.customers import generate_deposit_customers
from balance_pricing_engine.synthetic.response import deposit_uptake_probability


def segment(segment_id="external", opportunity_nzd=100_000, **kwargs):
    return DepositOpportunitySegment(
        segment_id=segment_id, opportunity_nzd=opportunity_nzd, **kwargs
    )


def profile(*segments, **kwargs):
    return DepositMarketProfile(
        profile_id="test-supply",
        version="1.0",
        opportunity_basis=kwargs.pop("opportunity_basis", "FLOW"),
        review_notes="Invented test assumptions.",
        segments=segments or (segment(),),
        **kwargs,
    )


def sample(*balances):
    return pd.DataFrame({"deposit_amount": balances})


@given(
    balances=st.lists(st.floats(min_value=1, max_value=1e6), min_size=1, max_size=15),
    repeats=st.integers(min_value=1, max_value=8),
    opportunity=st.floats(min_value=0, max_value=1e9),
)
@settings(max_examples=40)
def test_fixed_opportunity_normalisation_and_replication(balances, repeats, opportunity):
    """T01–05. Changing the quadrature count keeps the represented money fixed."""
    inputs = profile(segment(opportunity_nzd=opportunity))
    rows = sample(*balances)
    probabilities = np.linspace(0.1, 0.9, len(rows))
    initial = DepositSupply(inputs, rows)
    replicated = pd.concat([rows] * repeats, ignore_index=True)
    expanded = DepositSupply(inputs, replicated)

    assert (initial.weights >= 0).all()
    assert math.fsum(initial.weights) == pytest.approx(opportunity)
    assert math.fsum(expanded.weights) == pytest.approx(opportunity)
    assert not initial.weights.flags.writeable
    expected = opportunity * np.dot(balances, probabilities) / sum(balances)
    assert initial.offer(rows, probabilities, 0.04).gross == pytest.approx(expected)
    assert expanded.offer(replicated, np.tile(probabilities, repeats), 0.04).gross == pytest.approx(
        expected
    )


def test_independent_sample_sizes_converge_without_expanding_capacity():
    """T04, T41. Compare bookings with an independent 200k-row reference."""
    inputs = profile()

    def bookings(n, seed):
        rows = generate_deposit_customers(n, np.random.default_rng(seed))
        supply = DepositSupply(inputs, rows)
        probability = deposit_uptake_probability(rows, 0.04, 0.041, 28, 20)
        assert supply.eligible_opportunity == 100_000
        assert np.sum(supply.weights) == pytest.approx(100_000)
        return supply.offer(rows, probability, 0.04).gross

    reference = bookings(200_000, 99)
    small = np.array([bookings(1_000, seed) for seed in range(8)])
    large = np.array([bookings(25_000, seed) for seed in range(8)])
    assert abs(large.mean() - reference) < 200
    assert np.mean((large - reference) ** 2) < np.mean((small - reference) ** 2)


@pytest.mark.parametrize(
    ("opportunity", "conversion", "probability", "bookings"),
    [(0, 1, 1, 0), (100, 1, 1, 25), (100, 0, 1, 0), (100, 1, 0, 0), (100, 4, 0.8, 25)],
)
def test_saturation_and_zero_opportunity(opportunity, conversion, probability, bookings):
    """T06–08, T24. All probabilities and booked capacity are bounded."""
    inputs = profile(
        segment(
            opportunity_nzd=opportunity,
            reachable_share=0.5,
            eligible_participation=0.5,
            conversion_multiplier=conversion,
        )
    )
    rows = sample(1, 2, 3)
    supply = DepositSupply(inputs, rows)
    assert supply.offer(rows, np.repeat(probability, 3), 0.04).gross == pytest.approx(bookings)
    assert supply.offer(rows, np.ones(3), 0.04).gross <= supply.eligible_opportunity


@pytest.mark.parametrize(
    "kwargs",
    [
        {"opportunity_nzd": -1},
        {"reachable_share": 1.01},
        {"eligible_participation": -0.1},
        {"eligible_participation": float("nan")},
        {"maturity_runoff_probability": float("inf")},
        {"funding_eligibility": 1.01},
        {"balance_min": 10, "balance_max": 5},
        {"stock_maturity_fraction": -0.5},
        {"conversion_multiplier": -0.1},
        {"acquisition_cost_rate": float("inf")},
        {"source_interest_rate": -0.01},
        {"maturity_runoff_probability": 0.4},
    ],
)
def test_invalid_numbers_fail_loudly(kwargs):
    """T09, T10, T37. The config validates even copy updates."""
    with pytest.raises(ValidationError):
        segment().model_copy(update=kwargs)


def test_normalisation_rejects_missing_representative_balances():
    rows = sample(0, 0)
    with pytest.raises(ValueError, match="no positive sample balance"):
        DepositSupply(profile(), rows)
    assert DepositSupply(profile(segment(opportunity_nzd=0)), rows).weights.tolist() == [0, 0]
    with pytest.raises(ValueError, match="finite and non-negative"):
        DepositSupply(profile(), sample(-1, 1))
    with pytest.raises(ValueError, match="finite and non-negative"):
        DepositSupply(profile(), sample(float("nan"), 1))


def test_sample_and_offer_validation():
    rows = sample(1, 2)
    supply = DepositSupply(profile(), rows)
    for invalid in (np.array([1]), np.array([1, 1.1]), np.array([1, np.nan])):
        with pytest.raises(ValueError, match="probabilities"):
            supply.offer(rows, invalid, 0.04)
    with pytest.raises(ValueError, match="non-negative"):
        supply.offer(rows, np.ones(2), -0.04)
    with pytest.raises(ValueError, match="sample"):
        supply.offer(rows.set_axis([5, 6]), np.ones(2), 0.04)
    with pytest.raises(ValueError, match="unique"):
        DepositSupply(profile(), rows.set_axis([1, 1]))
    with pytest.raises(ValueError, match="unique"):
        supply.offer(rows.iloc[[0, 0]], np.ones(2), 0.04)


def test_stock_must_be_converted_to_horizon_flows():
    pool = segment(stock_maturity_fraction=0.25)
    with pytest.raises(ValidationError, match="FLOW"):
        profile(pool)
    with pytest.raises(ValidationError, match="STOCK"):
        profile(opportunity_basis="STOCK")
    rows = sample(1, 2)
    supply = DepositSupply(profile(pool, opportunity_basis="STOCK"), rows)
    estimate = supply.estimate([supply.offer(rows, np.ones(2), 0.04)])
    assert estimate.addressable_flow == 25_000
    assert estimate.eligible_opportunity == 25_000
    assert estimate.gross_bookings == pytest.approx(25_000)


def test_profile_rejects_overlaps_including_wildcards_and_duplicate_ids():
    """T11. Pools may share numerical samples only across disjoint economic classes."""
    with pytest.raises(ValidationError, match="overlap"):
        profile(segment(balance_max=20), segment("other", balance_min=5, channel="digital"))
    with pytest.raises(ValidationError, match="unique"):
        profile(segment(balance_max=20), segment(balance_min=20))
    with pytest.raises(ValidationError, match="comparable"):
        profile(segment(), segment("six-month", term_months=6))
    assert (
        len(
            profile(
                segment(channel="digital"),
                segment("assisted", channel="assisted"),
            ).segments
        )
        == 2
    )
    assert (
        len(
            profile(
                segment(customer_segment="short_relationship"),
                segment("established", customer_segment="established"),
            ).segments
        )
        == 2
    )


def test_tier_membership_and_behaviour_features_stay_raw():
    """T12–13. A weighted NZD 10k prospect stays under a NZD 20k tier."""
    rows = sample(10_000, 100_000)
    rows["digital_engagement"] = [0.75, 0.25]
    rows["relationship_years"] = [2, 10]
    rows["rate_sensitivity"] = [0.7, 0.4]
    before = rows.copy()
    probability = deposit_uptake_probability(rows, 0.04, 0.041, 28, 20)
    supply = DepositSupply(
        profile(
            segment("small", opportunity_nzd=500_000, balance_max=20_000, channel="digital"),
            segment(
                "large",
                opportunity_nzd=900_000,
                balance_min=20_000,
                customer_segment="established",
            ),
        ),
        rows,
    )

    assert supply.weights.tolist() == [500_000, 900_000]
    pd.testing.assert_frame_equal(rows, before)
    np.testing.assert_array_equal(
        probability, deposit_uptake_probability(rows, 0.04, 0.041, 28, 20)
    )
    low = supply.offer(rows.iloc[:1], probability[:1], 0.04)
    high = supply.offer(rows.iloc[1:], probability[1:], 0.05)
    assert low.gross == pytest.approx(500_000 * probability[0])
    assert high.gross == pytest.approx(900_000 * probability[1])


def test_financial_flow_and_funding_reconcile_by_hand():
    """T14–23. Retention prevents distinct runoff. Internal transfers preserve funding."""
    rows = sample(10_000)
    inputs = profile(
        segment(
            "external",
            100,
            funding_eligibility=0.8,
            acquisition_cost_rate=0.01,
        ),
        segment(
            "retained",
            200,
            flow=DepositFlow.RETAINED_MATURITY,
            maturity_runoff_probability=0.5,
            funding_eligibility=0.9,
            source_interest_rate=0.03,
            acquisition_cost_rate=0.02,
        ),
        segment("migration", 80, flow=DepositFlow.INTERNAL_MIGRATION, source_interest_rate=0.02),
        segment("existing", 40, flow=DepositFlow.EXISTING_BALANCE, source_interest_rate=0.01),
        opening_funding_nzd=500,
        opening_unallocated_interest_rate=0.04,
    )
    supply = DepositSupply(inputs, rows)
    offer = supply.offer(rows, np.array([0.5]), 0.06)
    evidence = supply.estimate([offer])

    assert [row.gross_booked for row in evidence.flows] == [50, 100, 40, 20]
    assert evidence.gross_bookings == 210
    assert evidence.existing_funding_displaced == 110
    assert evidence.net_incremental_funding == 100
    assert evidence.eligible_incremental_funding == 85
    assert evidence.eligible_incremental_opportunity == 170
    assert evidence.eligible_incremental_funding <= evidence.eligible_incremental_opportunity
    assert evidence.reference_funding == 400
    assert evidence.counterfactual_runoff == 100
    assert evidence.actual_runoff == 50
    assert evidence.active_funding == 500
    assert evidence.usable_funding == 485
    assert evidence.gross_interest_cost == pytest.approx(12.6)
    assert evidence.counterfactual_source_interest_cost == pytest.approx(2.5)
    assert evidence.incremental_interest_cost == pytest.approx(10.1)
    assert evidence.reference_interest_cost == pytest.approx(12.2)
    assert evidence.active_interest_cost == pytest.approx(22.3)
    assert evidence.acquisition_cost == pytest.approx(2.5)
    assert evidence.unsupported_funding == 15

    assert expected_wholesale_funding_cost(600, evidence.usable_funding, 0.05) == 5.75
    funding = calculate_funding_result(600, evidence.usable_funding, 0.9, 0.02)
    assert funding.funding_shortfall == 55
    assert funding.funding_penalty == pytest.approx(1.1)
    assert calculate_funding_result(600, evidence.usable_funding, 0, 0.02).funding_penalty == 0
    assert expected_excess_deposit_income(400, evidence.usable_funding, 0.04) == pytest.approx(3.4)


def test_retention_without_runoff_evidence_cannot_add_funding():
    rows = sample(1)
    pool = segment(flow=DepositFlow.RETAINED_MATURITY, source_interest_rate=0.04)
    with pytest.raises(ValidationError, match="Opening"):
        profile(pool)
    supply = DepositSupply(profile(pool, opening_funding_nzd=100_000), rows)
    estimate = supply.estimate([supply.offer(rows, np.ones(1), 0.05)])
    assert estimate.gross_bookings == 100_000
    assert estimate.net_incremental_funding == 0
    assert estimate.eligible_incremental_funding == 0
    assert estimate.usable_funding == 100_000
    assert estimate.unsupported_funding == 100_000
    assert estimate.active_interest_cost == 5_000


@pytest.mark.parametrize(
    ("mode", "penalty_rate", "usable", "constrained", "penalty"),
    [
        (ConcentrationMode.HARD_LIMIT, 0, 80, 20, 0),
        (ConcentrationMode.SOFT_PENALTY, 0.1, 100, 0, 2),
        (ConcentrationMode.DIAGNOSTIC_ONLY, 0, 100, 0, 0),
    ],
)
def test_concentration_modes_and_vectorised_funding(
    mode, penalty_rate, usable, constrained, penalty
):
    """T25–26. One portfolio concentration rule applies after offers are aggregated."""
    rows = sample(10, 10)
    inputs = profile(
        segment(opportunity_nzd=100),
        concentration=DepositConcentrationPolicy(
            mode=mode, limit_nzd=80, penalty_rate=penalty_rate
        ),
    )
    supply = DepositSupply(inputs, rows)
    offers = [supply.offer(rows.iloc[[i]], np.ones(1), 0.05) for i in range(2)]
    estimate = supply.estimate(offers)
    portfolio = supply.portfolio(np.array([50, 100]), np.array([50, 100]), np.array([2.5, 5]), 0)

    assert estimate.gross_bookings == 100
    assert estimate.net_incremental_funding == 100
    assert estimate.usable_funding == usable
    assert estimate.constrained_funding == constrained
    assert estimate.concentration_penalty == penalty
    assert estimate.concentration_excess == 20
    assert estimate.concentration_binding
    assert estimate.active_interest_cost == 5
    np.testing.assert_array_equal(portfolio.usable_funding, [50, usable])
    np.testing.assert_array_equal(portfolio.concentration_penalty, [0, penalty])


def test_lower_offer_reach_does_not_reduce_existing_maturities_or_reference_runoff():
    rows = sample(1)
    maturities = segment(
        opportunity_nzd=100,
        flow=DepositFlow.RETAINED_MATURITY,
        maturity_runoff_probability=1,
        reachable_share=0.5,
        source_interest_rate=0.04,
    )
    supply = DepositSupply(profile(maturities, opening_funding_nzd=100), rows)
    retained = supply.estimate([supply.offer(rows, np.ones(1), 0.04)])
    assert retained.counterfactual_runoff == 100
    assert retained.actual_runoff == 50
    assert retained.active_funding == 50
    no_retention = supply.estimate([supply.offer(rows, np.zeros(1), 0.04)])
    assert no_retention.actual_runoff == 100
    assert no_retention.active_funding == 0
    assert no_retention.usable_funding == 0
    assert no_retention.active_interest_cost == 0


def test_evidence_rejects_rebooking_a_sample_across_pricing_tiers():
    inputs = profile()
    rows = sample(1, 1)
    supply = DepositSupply(inputs, rows)
    offer = supply.offer(rows, np.ones(2), 0.04)
    with pytest.raises(ValueError, match="same sample"):
        supply.estimate([offer, offer])
    with pytest.raises(ValueError, match="from this deposit supply"):
        DepositSupply(inputs, rows).estimate([offer])
    with pytest.raises(ValueError, match="from this deposit supply"):
        supply.estimate([])


def test_exact_balance_boundary_matches_pricing_tier():
    from balance_pricing_engine.config import DepositTierConfig, GridConfig
    from balance_pricing_engine.optimization.segmented import segment_deposit_customers

    rows = sample(10_000, 20_000)
    grid = GridConfig(start=0.04, stop=0.05, step=0.01)
    tiers = segment_deposit_customers(
        rows,
        [
            DepositTierConfig(name="small", balance_max=20_000, rate_grid=grid),
            DepositTierConfig(name="large", rate_grid=grid),
        ],
    )
    inputs = profile(
        segment("small", 500_000, balance_max=20_000),
        segment("large", 100_000, balance_min=20_000),
    )
    supply = DepositSupply(inputs, rows)
    small = supply.offer(tiers["small"], np.ones(1), 0.04)
    large = supply.offer(tiers["large"], np.ones(1), 0.05)
    assert small.gross == 500_000
    assert large.gross == 100_000


def test_finite_inputs_do_not_allow_unrepresentable_opening_plus_inflow():
    with pytest.raises(ValidationError, match="finite"):
        profile(segment(opportunity_nzd=1e308), opening_funding_nzd=1e308)
    with pytest.raises(ValidationError, match="finite"):
        profile(
            segment("low", 1e308, balance_max=10),
            segment("high", 1e308, balance_min=10),
        )


def test_missing_or_invalid_stratification_features_fail_loudly():
    rows = sample(10_000)
    inputs = profile(segment(channel="digital"))
    with pytest.raises(ValueError, match="must contain digital_engagement"):
        DepositSupply(inputs, rows)
    rows["digital_engagement"] = 1.1
    with pytest.raises(ValueError, match="digital_engagement must be finite"):
        DepositSupply(inputs, rows)


def test_maturity_mismatch_disclosed_for_term_shorter_than_horizon():
    rows = sample(10_000)
    supply = DepositSupply(profile(segment(term_months=6)), rows)
    assert supply.estimate([supply.offer(rows, np.ones(1), 0.04)]).maturity_mismatch


def test_multi_tier_full_conversion_stays_within_pool_and_active_interest_is_nonnegative():
    rng = np.random.default_rng(122)
    # This fixed sample exposes a one-ULP excess when ten tier sums are rounded.
    rows = sample(*rng.uniform(1, 1e6, (5, 5000))[-1])
    migration = segment(
        opportunity_nzd=200_000_000, flow=DepositFlow.INTERNAL_MIGRATION, source_interest_rate=0.05
    )
    supply = DepositSupply(profile(migration, opening_funding_nzd=200_000_000), rows)
    offers = [
        supply.offer(rows.iloc[i : i + 500], np.ones(500), 0) for i in range(0, len(rows), 500)
    ]
    estimate = supply.estimate(offers)
    assert estimate.gross_bookings == estimate.eligible_opportunity == 200_000_000
    assert estimate.active_interest_cost == 0
    assert estimate.usable_funding == 200_000_000
    position = supply.portfolio(
        np.array([200_000_000.00000003, 190_000_000]),
        np.zeros(2),
        np.array([-10_000_000.000000002, -9_500_000]),
        np.zeros(2),
    )
    np.testing.assert_array_equal(position.gross, [200_000_000, 190_000_000])
    np.testing.assert_array_equal(position.deposit_cost, [0, 500_000])


def test_ineligible_external_inflow_has_expense_and_no_usable_funding_credit():
    rows = sample(100)
    inputs = profile(
        segment(opportunity_nzd=100, funding_eligibility=0, acquisition_cost_rate=0.01),
        opening_funding_nzd=200,
        opening_unallocated_interest_rate=0.03,
    )
    supply = DepositSupply(inputs, rows)
    evidence = supply.estimate([supply.offer(rows, np.ones(1), 0.05)])
    assert evidence.net_incremental_funding == 100
    assert evidence.active_funding == 300
    assert evidence.usable_funding == 200
    assert evidence.unsupported_funding == 100
    assert evidence.reference_interest_cost == 6
    assert evidence.active_interest_cost == 11
    assert evidence.acquisition_cost == 1

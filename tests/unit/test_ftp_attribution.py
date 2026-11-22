"""FIN-003 (Phase 4d): funding attribution, transfer elimination and economic ROE."""

from __future__ import annotations

from datetime import datetime, timezone
import math

from hypothesis import given
from hypothesis import strategies as st
import pytest

from balance_pricing_engine.optimization.grid_search import (
    DepositSupplyOptimizationResult,
    OptimizationResult,
)
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.economic_roe import (
    NOT_MODELLED,
    EconomicROEBridge,
    economic_roe_bridge,
)
from balance_pricing_engine.pricing.funds_transfer_pricing import (
    FTPAllocationLedger,
    FTPMarketState,
    FTPRateSet,
    ftp_market,
    illustrative_curve,
    illustrative_funding_profile,
    ledger_from_result,
)
from balance_pricing_engine.pricing.profit import (
    expected_excess_deposit_income,
    expected_wholesale_funding_cost,
    portfolio_return_on_equity,
)
from balance_pricing_engine.synthetic.market import MarketState

AS_OF = datetime(2026, 11, 20, tzinfo=timezone.utc)
MONEY = st.floats(0, 2e9, allow_nan=False, allow_infinity=False)
RATE = st.floats(0, 0.15, allow_nan=False, allow_infinity=False)
TOL = dict(rel=1e-9, abs=1e-6)


def _ledger(**update) -> FTPAllocationLedger:
    data = dict(
        loan_volume=500e6,
        usable_deposit_funding=320e6,
        loan_ftp_rate=0.0416,
        deposit_ftp_rate=0.037,
        wholesale_funding_rate=0.037,
        surplus_reinvestment_rate=0.026,
        loan_interest_income=31e6,
        expected_credit_loss=3.4e6,
        deposit_interest_cost=13e6,
        deposit_acquisition_cost=0.2e6,
    )
    data.update(update)
    return FTPAllocationLedger(**data)


def _result(**update) -> OptimizationResult:
    data = dict(
        loan_rate=0.0619,
        deposit_rate=0.0405,
        loan_interest_income=31e6,
        expected_credit_loss=3.4e6,
        deposit_interest_cost=13e6,
        wholesale_funding_cost=180e6 * 0.037,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=(0.65 * 500e6 - 320e6) * 0.018,
        total_objective=0.0,
        expected_loan_volume=500e6,
        expected_deposit_volume=320e6,
        funding_shortfall=0.65 * 500e6 - 320e6,
        capital_charge=500e6 * 0.35 * 0.1 * 0.12,
    )
    data.update(update)
    objective = (
        data["loan_interest_income"]
        - data["expected_credit_loss"]
        - data["deposit_interest_cost"]
        - data["wholesale_funding_cost"]
        + data["excess_deposit_income"]
        - data["funding_shortfall_penalty"]
        - data["capital_charge"]
    )
    data["total_objective"] = update.get("total_objective", objective)
    return OptimizationResult(**data)


def _rates(wholesale=0.037, surplus=0.026, loan=0.0416, deposit=0.037) -> FTPRateSet:
    return FTPRateSet(
        curve_key="c@1#t",
        profile_key="p@1",
        loan_reference_rate=loan - 0.01,
        loan_liquidity_rate=0.01,
        loan_ftp_rate=loan,
        deposit_ftp_rate=deposit,
        wholesale_funding_rate=wholesale,
        surplus_reinvestment_rate=surplus,
        quotes=(),
    )


# -- Funding reconciliation (T13-T19) ----------------------------------------------------


def test_t13_wholesale_deficit_reconciles():
    ledger = _ledger()
    assert ledger.wholesale_deficit == 180e6
    assert ledger.surplus_funds == 0.0
    assert ledger.external_wholesale_funding_cost == pytest.approx(180e6 * 0.037)
    assert ledger.external_wholesale_funding_cost == expected_wholesale_funding_cost(
        500e6, 320e6, 0.037
    )


def test_t14_surplus_funding_reconciles():
    ledger = _ledger(usable_deposit_funding=650e6)
    assert ledger.wholesale_deficit == 0.0
    assert ledger.surplus_funds == 150e6
    assert ledger.surplus_investment_income == expected_excess_deposit_income(500e6, 650e6, 0.026)


def test_t15_funding_terms_are_aggregated_before_nonlinear_penalties():
    # Two cohorts: one in deficit, one in surplus. The shared position nets them first.
    cohorts = ((300e6, 100e6), (200e6, 400e6))
    loans = sum(loan for loan, _ in cohorts)
    deposits = sum(deposit for _, deposit in cohorts)
    aggregate = _ledger(loan_volume=loans, usable_deposit_funding=deposits)
    per_cohort = sum(
        _ledger(loan_volume=loan, usable_deposit_funding=deposit).external_wholesale_funding_cost
        for loan, deposit in cohorts
    )
    assert aggregate.external_wholesale_funding_cost == 0.0
    assert per_cohort > 0
    penalty = calculate_funding_result(loans, deposits, 0.65, 0.018).funding_penalty
    summed = sum(
        calculate_funding_result(loan, deposit, 0.65, 0.018).funding_penalty
        for loan, deposit in cohorts
    )
    assert penalty == 0.0 < summed


def test_t16_liquidity_premium_reaches_the_consolidated_result_once():
    # Raising the internal loan FTP (its liquidity component) moves only attribution.
    base = _ledger()
    charged = _ledger(loan_ftp_rate=base.loan_ftp_rate + 0.005)
    assert charged.consolidated_contribution == pytest.approx(base.consolidated_contribution)
    assert charged.loan_business_contribution == pytest.approx(
        base.loan_business_contribution - 500e6 * 0.005
    )
    assert charged.treasury_contribution == pytest.approx(
        base.treasury_contribution + 500e6 * 0.005
    )


def test_t17_deposit_interest_is_charged_once():
    ledger = _ledger()
    lines = (
        ledger.loan_business_contribution,
        ledger.deposit_business_contribution,
        ledger.treasury_contribution,
    )
    more_interest = _ledger(deposit_interest_cost=ledger.deposit_interest_cost + 1e6)
    assert more_interest.consolidated_contribution == pytest.approx(math.fsum(lines) - 1e6, **TOL)
    assert more_interest.treasury_contribution == ledger.treasury_contribution
    assert more_interest.loan_business_contribution == ledger.loan_business_contribution


def test_t18_steering_penalty_is_separate_from_the_ftp_ledger():
    result = _result()
    ledger = ledger_from_result(result, _rates())
    assert "funding_shortfall_penalty" not in ledger.to_dict()
    bridge = economic_roe_bridge(result, rwa_weight=0.35)
    assert bridge.economic_profit_before_capital == pytest.approx(
        ledger.consolidated_contribution, **TOL
    )
    assert bridge.model_objective == pytest.approx(result.total_objective, **TOL)


def test_t19_ledger_rejects_a_result_priced_at_another_external_rate():
    with pytest.raises(ValueError, match="wholesale funding cost"):
        ledger_from_result(_result(), _rates(wholesale=0.04))
    surplus = _result(
        expected_deposit_volume=650e6,
        wholesale_funding_cost=0.0,
        excess_deposit_income=150e6 * 0.025,
        funding_shortfall_penalty=0.0,
        funding_shortfall=0.0,
    )
    with pytest.raises(ValueError, match="surplus income"):
        ledger_from_result(surplus, _rates())
    assert ledger_from_result(surplus, _rates(surplus=0.025)).surplus_funds == 150e6


def test_supply_result_uses_recognised_funding_and_acquisition_cost():
    base = _result()
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields.update(
        expected_deposit_volume=900e6,
        usable_deposit_funding=320e6,
        deposit_acquisition_cost=0.2e6,
        deposit_concentration_penalty=0.1e6,
        total_objective=base.total_objective - 0.3e6,
    )
    result = DepositSupplyOptimizationResult(**fields)
    ledger = ledger_from_result(result, _rates())
    assert ledger.usable_deposit_funding == 320e6
    assert ledger.deposit_ftp_credit == 320e6 * 0.037
    assert ledger.deposit_acquisition_cost == 0.2e6
    bridge = economic_roe_bridge(result, rwa_weight=0.35)
    assert bridge.concentration_penalty == 0.1e6
    assert bridge.steering_penalties == pytest.approx(result.funding_shortfall_penalty + 0.1e6)


# -- Transfer pricing (T23-T27) ----------------------------------------------------------


@given(
    loans=MONEY,
    deposits=MONEY,
    loan_ftp=RATE,
    deposit_ftp=RATE,
    wholesale=RATE,
    surplus=RATE,
    interest=MONEY,
    loss=MONEY,
    deposit_cost=MONEY,
    acquisition=MONEY,
)
def test_t23_t26_transfers_cancel_and_consolidation_is_external(
    loans,
    deposits,
    loan_ftp,
    deposit_ftp,
    wholesale,
    surplus,
    interest,
    loss,
    deposit_cost,
    acquisition,
):
    ledger = FTPAllocationLedger(
        loan_volume=loans,
        usable_deposit_funding=deposits,
        loan_ftp_rate=loan_ftp,
        deposit_ftp_rate=deposit_ftp,
        wholesale_funding_rate=wholesale,
        surplus_reinvestment_rate=surplus,
        loan_interest_income=interest,
        expected_credit_loss=loss,
        deposit_interest_cost=deposit_cost,
        deposit_acquisition_cost=acquisition,
    )
    scale = max(1.0, interest, loss, deposit_cost, acquisition, loans * 0.15, deposits * 0.15)
    assert ledger.internal_transfer_net == 0.0
    assert abs(ledger.reconciliation_difference) <= 1e-12 * scale * 8
    # T24/T25: each side of every transfer is equal and opposite.
    allocations = dict(((unit, line), amount) for unit, line, amount in ledger.internal_allocations)
    assert (
        allocations[("loan_business", "loan_ftp_charge")]
        == -allocations[("treasury", "loan_ftp_receipt")]
    )
    assert (
        allocations[("deposit_business", "deposit_ftp_credit")]
        == -allocations[("treasury", "deposit_ftp_payment")]
    )


def test_t24_t25_charges_equal_the_treasury_counterparty():
    ledger = _ledger()
    assert ledger.loan_ftp_charge == ledger.treasury_ftp_receipts == 500e6 * 0.0416
    assert ledger.deposit_ftp_credit == ledger.treasury_ftp_payments == 320e6 * 0.037


def test_t27_transfers_do_not_duplicate_wholesale_funding_cost():
    ledger = _ledger()
    external_costs = (
        ledger.expected_credit_loss
        + ledger.deposit_interest_cost
        + ledger.deposit_acquisition_cost
        + ledger.external_wholesale_funding_cost
    )
    assert ledger.consolidated_contribution == pytest.approx(
        ledger.loan_interest_income - external_costs + ledger.surplus_investment_income, **TOL
    )
    # The loan's FTP charge is never also deducted as an external cost.
    assert ledger.consolidated_contribution > ledger.loan_interest_income - external_costs - (
        ledger.loan_ftp_charge
    )


def test_ledger_rejects_non_finite_or_negative_volumes():
    with pytest.raises(ValueError, match="finite"):
        _ledger(loan_ftp_rate=math.nan)
    with pytest.raises(ValueError, match="non-negative"):
        _ledger(loan_volume=-1.0)


# -- Economic ROE (T29-T35) --------------------------------------------------------------


def test_t29_t31_t32_bridge_reconciles_and_preserves_model_roe():
    result = _result()
    bridge = economic_roe_bridge(result, rwa_weight=0.35)
    expected_model = portfolio_return_on_equity(
        result.loan_interest_income,
        result.expected_credit_loss,
        result.deposit_interest_cost,
        result.wholesale_funding_cost,
        result.funding_shortfall_penalty,
        result.excess_deposit_income,
        result.expected_loan_volume,
        0.35,
    )
    assert bridge.model_return_on_equity == expected_model
    assert bridge.required_capital == pytest.approx(500e6 * 0.35 * 0.1)
    payload = bridge.to_dict()
    assert payload["capital_denominator"]["required_capital"] == bridge.required_capital
    numerator = payload["numerator_bridge"]
    assert numerator["model_objective"] == pytest.approx(result.total_objective, **TOL)
    assert math.fsum(
        numerator[name]
        for name in (
            "economic_profit_before_capital",
            "funding_shortfall_penalty",
            "concentration_penalty",
            "capital_charge",
        )
    ) == pytest.approx(result.total_objective, **TOL)
    assert payload["excluded_operating_expenses"] == payload["excluded_tax"] == NOT_MODELLED


def test_t30_economic_roe_adds_back_only_steering_penalties():
    result = _result()
    bridge = economic_roe_bridge(result, rwa_weight=0.35)
    assert bridge.economic_return_on_equity == pytest.approx(
        bridge.model_return_on_equity + result.funding_shortfall_penalty / bridge.required_capital
    )
    assert bridge.steering_roe_difference > 0
    no_penalty = economic_roe_bridge(
        _result(funding_shortfall_penalty=0.0, funding_shortfall=0.0), rwa_weight=0.35
    )
    assert no_penalty.economic_return_on_equity == pytest.approx(no_penalty.model_return_on_equity)


def test_t33_zero_capital_gives_unavailable_economic_roe_and_legacy_zero_model_roe():
    result = _result(
        expected_loan_volume=0.0,
        loan_interest_income=0.0,
        expected_credit_loss=0.0,
        wholesale_funding_cost=0.0,
        excess_deposit_income=320e6 * 0.025,
        funding_shortfall_penalty=0.0,
        funding_shortfall=0.0,
        capital_charge=0.0,
    )
    bridge = economic_roe_bridge(result, rwa_weight=0.35)
    assert bridge.required_capital == 0.0
    assert bridge.economic_return_on_equity is None
    assert bridge.steering_roe_difference is None
    assert bridge.model_return_on_equity == 0.0
    assert bridge.to_dict()["economic_return_on_equity"] is None


def test_bridge_refuses_a_result_whose_objective_does_not_reconcile():
    with pytest.raises(ValueError, match="does not reconcile"):
        economic_roe_bridge(_result(total_objective=1.0), rwa_weight=0.35)
    with pytest.raises(ValueError, match="finite"):
        EconomicROEBridge(
            loan_interest_income=math.inf,
            expected_credit_loss=0,
            deposit_interest_cost=0,
            deposit_acquisition_cost=0,
            external_wholesale_funding_cost=0,
            surplus_investment_income=0,
            funding_shortfall_penalty=0,
            concentration_penalty=0,
            capital_charge=0,
            loan_volume=0,
            rwa_weight=0.35,
        )


@given(
    interest=MONEY,
    loss=MONEY,
    deposit=MONEY,
    wholesale=MONEY,
    surplus=MONEY,
    shortfall=MONEY,
    loans=st.floats(1e3, 2e9),
)
def test_economic_roe_never_below_model_roe(
    interest, loss, deposit, wholesale, surplus, shortfall, loans
):
    bridge = EconomicROEBridge(
        loan_interest_income=interest,
        expected_credit_loss=loss,
        deposit_interest_cost=deposit,
        deposit_acquisition_cost=0.0,
        external_wholesale_funding_cost=wholesale,
        surplus_investment_income=surplus,
        funding_shortfall_penalty=shortfall,
        concentration_penalty=0.0,
        capital_charge=0.0,
        loan_volume=loans,
        rwa_weight=0.35,
    )
    capital = bridge.required_capital
    assert bridge.economic_return_on_equity >= bridge.model_return_on_equity - 1e-12 * max(
        1.0, (interest + loss + deposit + wholesale + surplus + shortfall) / capital
    )


# -- FTP market ---------------------------------------------------------------------------


def test_ftp_market_changes_only_external_funding_rates():
    market = MarketState(
        cash_rate=0.025,
        wholesale_funding_spread=0.012,
        competitor_loan_rate=0.0499,
        competitor_deposit_rate=0.039,
        home_loan_reference_rate=0.0499,
        term_deposit_reference_rate=0.039,
    )
    rates = illustrative_funding_profile(wholesale_funding_tenor_months=36).rates(
        illustrative_curve(AS_OF)
    )
    priced = ftp_market(market, rates)
    assert isinstance(priced, FTPMarketState) and isinstance(priced, MarketState)
    assert priced.cash_rate == market.cash_rate
    assert priced.competitor_loan_rate == market.competitor_loan_rate
    assert (
        priced.wholesale_funding_rate
        == rates.wholesale_funding_rate
        != market.wholesale_funding_rate
    )
    assert priced.surplus_reinvestment_rate == rates.surplus_reinvestment_rate
    assert market.surplus_reinvestment_rate == market.cash_rate
    with pytest.raises(ValueError, match="finite"):
        FTPMarketState(
            cash_rate=0.025,
            wholesale_funding_spread=0.012,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.04,
        )
    with pytest.raises(ValueError, match="identity"):
        FTPMarketState(
            cash_rate=0.025,
            wholesale_funding_spread=0.012,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.04,
            ftp_wholesale_funding_rate=0.04,
            ftp_surplus_reinvestment_rate=0.03,
        )
    with pytest.raises(ValueError, match="finite"):
        FTPRateSet(**{**rates.__dict__, "loan_ftp_rate": math.nan})

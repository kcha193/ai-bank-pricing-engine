"""Reporting helpers for pricing walkthroughs, examples, and notebooks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig


MONEY_COLUMNS = [
    "objective_value",
    "total_objective",
    "loan_interest_income",
    "expected_credit_loss",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "funding_shortfall_penalty",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "objective_change_vs_baseline",
]


def fmt_rate(value: Any) -> str:
    """Format a decimal rate as a percentage string."""
    if value is None or pd.isna(value):
        return "n/a"
    return f"{value:.2%}"


def fmt_bps(value: Any) -> str:
    """Format a basis-point value."""
    if value is None or pd.isna(value):
        return "n/a"
    return f"{value:,.0f} bps"


def fmt_money(value: Any) -> str:
    """Format a monetary value with no decimal places."""
    if value is None or pd.isna(value):
        return "n/a"
    return f"${value:,.0f}"


def show_rate_grid(name: str, rates: np.ndarray) -> pd.DataFrame:
    """Build a display table for one product's candidate rate grid."""
    return pd.DataFrame(
        {
            "grid": name,
            "rate_decimal": rates,
            "rate_percent": [fmt_rate(rate) for rate in rates],
        }
    )


def build_rate_grid_table(rate_grids: Mapping[str, np.ndarray]) -> pd.DataFrame:
    """Build one display table from named candidate rate grids."""
    return pd.concat(
        [show_rate_grid(name, rates) for name, rates in rate_grids.items()],
        ignore_index=True,
    )


def format_result_table(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of a result table with rates, money, and spreads formatted."""
    formatted = df.copy()
    rate_columns = [column for column in formatted.columns if column.endswith("rate")]
    bps_columns = [column for column in formatted.columns if column.endswith("bps")]

    for column in rate_columns:
        formatted[column] = formatted[column].map(fmt_rate)
    for column in MONEY_COLUMNS:
        if column in formatted:
            formatted[column] = formatted[column].map(fmt_money)
    for column in bps_columns:
        formatted[column] = formatted[column].map(fmt_bps)
    return formatted


def build_config_summary(config: EngineConfig) -> pd.DataFrame:
    """Summarise the baseline configuration for walkthrough display."""
    section_explanations = {
        "simulation": "Defines the size of the synthetic customer portfolio used by the model.",
        "market": "Sets the external interest-rate environment the bank is pricing against.",
        "balance sheet": "Sets how the bank funds loans and how costly under-funding is.",
        "response": "Controls how customer demand reacts when rates move.",
    }

    def row(section: str, assumption: str, value: Any, assumption_explanation: str) -> dict[str, Any]:
        return {
            "section": section,
            "section_explanation": section_explanations[section],
            "assumption": assumption,
            "value": value,
            "assumption_explanation": assumption_explanation,
        }

    return pd.DataFrame(
        [
            row(
                "simulation",
                "loan customers",
                config.simulation.n_loan_customers,
                "Number of synthetic home loan customers used in the simulation.",
            ),
            row(
                "simulation",
                "deposit customers",
                config.simulation.n_deposit_customers,
                "Number of synthetic term deposit customers used in the simulation.",
            ),
            row(
                "market",
                "cash rate",
                fmt_rate(config.market.cash_rate),
                "Baseline central bank cash rate used as the starting point for market pricing.",
            ),
            row(
                "market",
                "wholesale funding spread",
                fmt_rate(config.market.wholesale_funding_spread),
                "Extra funding cost the bank pays above the cash rate when it needs wholesale funding.",
            ),
            row(
                "market",
                "competitor loan rate",
                fmt_rate(config.market.competitor_loan_rate),
                "Typical home loan rate offered by competitors, used to estimate how attractive this bank's loan rate is.",
            ),
            row(
                "market",
                "competitor deposit rate",
                fmt_rate(config.market.competitor_deposit_rate),
                "Typical term deposit rate offered by competitors, used to estimate how attractive this bank's deposit rate is.",
            ),
            row(
                "market",
                "home loan reference rate",
                fmt_rate(config.market.home_loan_reference_rate),
                "Starting home loan rate before the strategy grid tests higher or lower loan rates.",
            ),
            row(
                "market",
                "term deposit reference rate",
                fmt_rate(config.market.term_deposit_reference_rate),
                "Starting term deposit rate before the strategy grid tests higher or lower deposit rates.",
            ),
            row(
                "balance sheet",
                "required deposit funding ratio",
                fmt_rate(config.balance_sheet.required_deposit_funding_ratio),
                "Minimum share of expected loans the bank wants to fund with customer deposits.",
            ),
            row(
                "balance sheet",
                "funding shortfall penalty rate",
                fmt_rate(config.balance_sheet.funding_shortfall_penalty_rate),
                "Penalty rate applied when expected deposits are too low to meet the required funding ratio.",
            ),
            row(
                "response",
                "loan rate elasticity",
                config.response.loan_rate_elasticity,
                "How strongly expected home loan demand changes when this bank changes its loan rate.",
            ),
            row(
                "response",
                "deposit rate elasticity",
                config.response.deposit_rate_elasticity,
                "How strongly expected term deposit balances change when this bank changes its deposit rate.",
            ),
        ]
    )


def build_customer_profile_tables(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build descriptive profile tables for synthetic loan and deposit customers."""
    loan_profile_columns = [
        "loan_amount",
        "credit_score",
        "lvr",
        "income",
        "risk_score",
        "expected_loss_rate",
    ]
    deposit_profile_columns = [
        "deposit_amount",
        "relationship_years",
        "digital_engagement",
        "rate_sensitivity",
    ]
    return (
        loan_customers[loan_profile_columns].describe().T,
        deposit_customers[deposit_profile_columns].describe().T,
    )


def build_best_decision_table(result: Any) -> pd.DataFrame:
    """Build a one-row table for the selected pricing result."""
    return pd.DataFrame(
        [
            {
                "loan_rate": result.loan_rate,
                "deposit_rate": result.deposit_rate,
                "home_loan_spread_to_market_bps": result.home_loan_spread_to_market_bps,
                "term_deposit_spread_to_market_bps": result.term_deposit_spread_to_market_bps,
                "objective_value": result.objective_value,
                "expected_loan_volume": result.expected_loan_volume,
                "expected_deposit_volume": result.expected_deposit_volume,
                "funding_shortfall": result.funding_shortfall,
            }
        ]
    )


def build_objective_decomposition_table(decomposition: Any) -> pd.DataFrame:
    """Build a display table that reconciles objective components."""
    return pd.DataFrame(
        [
            {"component": "loan interest income", "amount": decomposition.loan_interest_income},
            {"component": "expected credit loss", "amount": -decomposition.expected_credit_loss},
            {"component": "deposit interest cost", "amount": -decomposition.deposit_interest_cost},
            {
                "component": "wholesale funding cost",
                "amount": -decomposition.wholesale_funding_cost,
            },
            {
                "component": "funding shortfall penalty",
                "amount": -decomposition.funding_shortfall_penalty,
            },
            {"component": "total objective", "amount": decomposition.total_objective},
        ]
    )


def build_best_by_loan_rate_table(grid_results: pd.DataFrame) -> pd.DataFrame:
    """Summarise the best deposit decision available at each home-loan rate."""
    return (
        grid_results.sort_values("total_objective", ascending=False)
        .groupby("loan_rate", as_index=False)
        .first()[
            [
                "loan_rate",
                "deposit_rate",
                "total_objective",
                "expected_loan_volume",
                "expected_deposit_volume",
                "funding_shortfall",
            ]
        ]
    )


def build_best_by_deposit_rate_table(grid_results: pd.DataFrame) -> pd.DataFrame:
    """Summarise the best loan decision available at each term-deposit rate."""
    return (
        grid_results.sort_values("total_objective", ascending=False)
        .groupby("deposit_rate", as_index=False)
        .first()[
            [
                "deposit_rate",
                "loan_rate",
                "total_objective",
                "expected_loan_volume",
                "expected_deposit_volume",
                "funding_shortfall",
            ]
        ]
    )


def build_sensitivity_change_table(sensitivity: pd.DataFrame) -> pd.DataFrame:
    """Add rate and objective movement versus the baseline sensitivity row."""
    sensitivity_summary = sensitivity.copy()
    sensitivity_summary["loan_rate_change_vs_baseline"] = (
        sensitivity_summary["best_loan_rate"] - sensitivity_summary.loc[0, "best_loan_rate"]
    )
    sensitivity_summary["deposit_rate_change_vs_baseline"] = (
        sensitivity_summary["best_deposit_rate"]
        - sensitivity_summary.loc[0, "best_deposit_rate"]
    )
    sensitivity_summary["objective_change_vs_baseline"] = (
        sensitivity_summary["objective_value"] - sensitivity_summary.loc[0, "objective_value"]
    )
    return sensitivity_summary


def build_sensitivity_summary_table(sensitivity: pd.DataFrame) -> pd.DataFrame:
    """Calculate and format sensitivity movement versus the baseline row."""
    sensitivity_summary = build_sensitivity_change_table(sensitivity)
    formatted = sensitivity_summary[
        [
            "scenario_name",
            "loan_rate_change_vs_baseline",
            "deposit_rate_change_vs_baseline",
            "objective_change_vs_baseline",
            "funding_shortfall",
            "boundary_warning_flag",
        ]
    ].copy()

    for column in ["loan_rate_change_vs_baseline", "deposit_rate_change_vs_baseline"]:
        formatted[column] = formatted[column].map(lambda value: f"{value * 10_000:+.0f} bps")
    formatted["objective_change_vs_baseline"] = formatted[
        "objective_change_vs_baseline"
    ].map(fmt_money)
    formatted["funding_shortfall"] = formatted["funding_shortfall"].map(fmt_money)
    return formatted


def build_scenario_comparison_table(baseline_result: Any, scenario_result: Any) -> pd.DataFrame:
    """Compare the baseline result with a changed-assumption scenario result."""
    return pd.DataFrame(
        [
            _scenario_result_row("baseline", baseline_result),
            _scenario_result_row("higher funding penalty", scenario_result),
        ]
    )


def _scenario_result_row(scenario_name: str, result: Any) -> dict[str, Any]:
    return {
        "scenario": scenario_name,
        "loan_rate": result.loan_rate,
        "deposit_rate": result.deposit_rate,
        "objective_value": result.objective_value,
        "expected_loan_volume": result.expected_loan_volume,
        "expected_deposit_volume": result.expected_deposit_volume,
        "funding_shortfall": result.funding_shortfall,
    }

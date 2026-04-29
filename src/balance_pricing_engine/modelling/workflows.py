"""Reusable workflows for synthetic response modelling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.reports import train_response_model_report
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng


@dataclass(frozen=True)
class ResponseModelWorkflowResult:
    """Datasets, rate grids, and reports produced by the response modelling workflow."""

    loan_dataset: pd.DataFrame
    deposit_dataset: pd.DataFrame
    loan_grid: list[float]
    deposit_grid: list[float]
    loan_model_report: dict[str, Any]
    deposit_model_report: dict[str, Any]


def run_response_model_workflow(config: EngineConfig) -> ResponseModelWorkflowResult:
    """Build synthetic response datasets and train the standard response model reports."""
    rng = make_rng(config.random_seed)
    loan_model_customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
    deposit_model_customers = generate_deposit_customers(
        config.simulation.n_deposit_customers,
        rng,
    )
    loan_grid_raw, deposit_grid_raw = build_configured_rate_grids(config)
    loan_grid = [float(rate) for rate in loan_grid_raw]
    deposit_grid = [float(rate) for rate in deposit_grid_raw]

    loan_dataset = build_loan_response_dataset(
        loan_model_customers,
        loan_grid_raw,
        config.market.competitor_loan_rate,
        config.response.loan_rate_elasticity,
        config.response.loan_competitor_gap_elasticity,
        rng,
        n_samples=config.simulation.n_loan_customers * 3,
    )
    deposit_dataset = build_deposit_response_dataset(
        deposit_model_customers,
        deposit_grid_raw,
        config.market.competitor_deposit_rate,
        config.response.deposit_rate_elasticity,
        config.response.deposit_competitor_gap_elasticity,
        rng,
        n_samples=config.simulation.n_deposit_customers * 3,
    )

    loan_model_report = train_response_model_report(
        title="Home loan acceptance model",
        dataset=loan_dataset,
        feature_columns=LOAN_FEATURE_COLUMNS,
        target_column=LOAN_TARGET_COLUMN,
        offered_rates=loan_grid,
        competitor_rate=config.market.competitor_loan_rate,
        expected_direction="decreasing",
        random_seed=config.random_seed,
    )
    deposit_model_report = train_response_model_report(
        title="Term deposit uptake model",
        dataset=deposit_dataset,
        feature_columns=DEPOSIT_FEATURE_COLUMNS,
        target_column=DEPOSIT_TARGET_COLUMN,
        offered_rates=deposit_grid,
        competitor_rate=config.market.competitor_deposit_rate,
        expected_direction="increasing",
        random_seed=config.random_seed,
    )

    return ResponseModelWorkflowResult(
        loan_dataset=loan_dataset,
        deposit_dataset=deposit_dataset,
        loan_grid=loan_grid,
        deposit_grid=deposit_grid,
        loan_model_report=loan_model_report,
        deposit_model_report=deposit_model_report,
    )

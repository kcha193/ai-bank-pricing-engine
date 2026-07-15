"""Local, file/in-memory-backed feature provider for synthetic diagnostic models.

``LocalFeatureStore`` is the only implementation of ``FeatureProvider`` today.
It wraps the existing synthetic customer generators and dataset builders in
``synthetic/customers.py`` and ``modelling/datasets.py`` rather than
introducing a new data source, so training features and the features scored at
optimizer time are demonstrably built from the same functions and schema.

A future managed feature store (Feast, a warehouse-backed store, etc.) can
implement ``FeatureProvider`` and be substituted without changing callers —
see docs/adr/0002-local-feature-store-interface.md.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.features.contracts import (
    FeatureLineage,
    FeatureSchema,
    utc_now_iso,
)
from balance_pricing_engine.features.definitions import feature_schema_for_product
from balance_pricing_engine.modelling.datasets import (
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)

_SOURCE_NAME = "synthetic_local_store"


class LocalFeatureStore:
    """Synthetic-diagnostic feature provider backed by in-repo generators."""

    def schema(self, product: str) -> FeatureSchema:
        return feature_schema_for_product(product)

    def training_dataset(
        self,
        product: str,
        *,
        config: EngineConfig,
        rng: np.random.Generator,
        as_of: str | None = None,
    ) -> tuple[pd.DataFrame, FeatureLineage]:
        """Build a schema-validated training dataset for 'loan' or 'deposit'."""
        schema = feature_schema_for_product(product)
        loan_grid, deposit_grid = build_configured_rate_grids(config)
        if product == "loan":
            customers = generate_loan_customers(config.simulation.n_loan_customers, rng)
            dataset = build_loan_response_dataset(
                customers,
                loan_grid,
                config.market.competitor_loan_rate,
                config.response.loan_rate_elasticity,
                config.response.loan_competitor_gap_elasticity,
                rng,
            )
        elif product == "deposit":
            customers = generate_deposit_customers(config.simulation.n_deposit_customers, rng)
            dataset = build_deposit_response_dataset(
                customers,
                deposit_grid,
                config.market.competitor_deposit_rate,
                config.response.deposit_rate_elasticity,
                config.response.deposit_competitor_gap_elasticity,
                rng,
            )
        else:
            raise ValueError(f"Unknown feature product scope: {product!r}.")

        schema.validate(dataset, require_target=True)
        lineage = FeatureLineage(
            feature_set_name=schema.name,
            feature_set_version=schema.version,
            schema_fingerprint=schema.fingerprint,
            product=product,
            as_of=as_of or utc_now_iso(),
            row_count=len(dataset),
            source=_SOURCE_NAME,
        )
        return dataset, lineage

    def serving_features(
        self,
        product: str,
        customers: pd.DataFrame,
        *,
        offered_rate: float | None = None,
        competitor_rate: float | None = None,
        as_of: str | None = None,
    ) -> tuple[pd.DataFrame, FeatureLineage]:
        """Return a schema-validated serving snapshot for lineage/diagnostics.

        This snapshot is used for lineage recording and compatibility checks,
        not to replace the optimizer's own per-rate scoring loop (see
        ``optimization/response_fn.MLResponseFnAdapter``), which still varies
        ``offered_rate`` across the full rate grid.
        """
        schema = feature_schema_for_product(product)
        frame = customers.copy()
        if offered_rate is not None:
            frame["offered_rate"] = offered_rate
        if competitor_rate is not None:
            frame["competitor_rate"] = competitor_rate
        if "offered_rate" in frame.columns and "competitor_rate" in frame.columns:
            frame["rate_gap_to_competitor"] = frame["offered_rate"] - frame["competitor_rate"]
        if product == "loan" and "risk_segment" not in frame.columns and "risk_score" in frame.columns:
            frame["risk_segment"] = _risk_segment(frame["risk_score"])
        if (
            product == "deposit"
            and "wealth_segment" not in frame.columns
            and "deposit_amount" in frame.columns
        ):
            frame["wealth_segment"] = _wealth_segment(frame["deposit_amount"])

        schema.validate(frame)
        lineage = FeatureLineage(
            feature_set_name=schema.name,
            feature_set_version=schema.version,
            schema_fingerprint=schema.fingerprint,
            product=product,
            as_of=as_of or utc_now_iso(),
            row_count=len(frame),
            source=_SOURCE_NAME,
        )
        return frame, lineage


def _risk_segment(risk_score: pd.Series) -> pd.Series:
    return pd.cut(risk_score, bins=[-np.inf, 0.33, 0.66, np.inf], labels=[0, 1, 2]).astype(int)


def _wealth_segment(deposit_amount: pd.Series) -> pd.Series:
    return pd.cut(
        deposit_amount, bins=[-np.inf, 50_000, 150_000, np.inf], labels=[0, 1, 2]
    ).astype(int)

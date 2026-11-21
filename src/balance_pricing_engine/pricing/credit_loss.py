"""Annual mortgage economic loss for explicit synthetic simulations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal, Optional

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike
from pydantic import ConfigDict, Field, StringConstraints, model_validator

from balance_pricing_engine.config import EconomicConfig

ANNUAL_EXPECTED_LOSS_COLUMN = "annual_expected_loss_nzd"
SCENARIO_WEIGHT_ABS_TOLERANCE = 1e-12
SYNTHETIC_LABEL = "SYNTHETIC / UNCALIBRATED"

_Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
_Probability = Annotated[float, Field(ge=0, le=1)]


class _LossConfig(EconomicConfig):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreditRiskBand(_LossConfig):
    """One band with an inclusive upper LVR. The last band is open-ended."""

    name: _Identifier
    upper_lvr: Optional[float] = Field(default=None, gt=0)


class CreditLossParameters(_LossConfig):
    """Conditional assumptions for one band in one economic scenario."""

    band_name: _Identifier
    pd: _Probability
    lgd: _Probability
    ead_multiplier: float = Field(ge=0)


class CreditLossScenario(_LossConfig):
    name: _Identifier
    weight: _Probability
    assumptions: tuple[CreditLossParameters, ...] = Field(min_length=1)


@dataclass(frozen=True)
class ScenarioLossEstimate:
    """Facility-level scenario evidence, aligned to the input row order."""

    name: str
    weight: float
    pd: np.ndarray
    lgd: np.ndarray
    ead_nzd: np.ndarray


@dataclass(frozen=True)
class CreditLossEstimate:
    """Facility-level annual loss conditional on booking, before offer acceptance."""

    principal_nzd: np.ndarray
    band_names: np.ndarray
    annual_expected_loss_nzd: np.ndarray
    scenarios: tuple[ScenarioLossEstimate, ...]


class CreditLossProfile(_LossConfig):
    """Versioned synthetic assumptions for annual economic loss."""

    version: _Identifier
    effective_date: date
    bands: tuple[CreditRiskBand, ...] = Field(min_length=1)
    unknown_band_name: _Identifier = "unknown"
    scenarios: tuple[CreditLossScenario, ...] = Field(min_length=1)
    model_basis: Literal["annual_economic_loss"] = "annual_economic_loss"
    pd_horizon_months: Literal[12] = 12
    lgd_definition: Literal["fractional_economic_loss_conditional_on_default"] = (
        "fractional_economic_loss_conditional_on_default"
    )
    ead_basis: Literal["booked_principal_times_scenario_multiplier_nzd"] = (
        "booked_principal_times_scenario_multiplier_nzd"
    )
    lvr_basis: Literal["origination"] = "origination"
    evidence_classification: Literal["illustrative"] = "illustrative"
    validation_status: Literal["validation_pending", "synthetic_validated"] = "validation_pending"
    review_note: str = Field(min_length=1)
    evidence_label: Literal["SYNTHETIC / UNCALIBRATED"] = SYNTHETIC_LABEL

    @model_validator(mode="after")
    def _validate_partition_and_scenarios(self) -> CreditLossProfile:
        names = self._band_names()
        if len(set(names)) != len(names):
            raise ValueError("LVR band names, including the unknown band, must be unique.")
        upper_bounds = [band.upper_lvr for band in self.bands]
        bounded_edges = [edge for edge in upper_bounds[:-1] if edge is not None]
        if upper_bounds[-1] is not None or len(bounded_edges) != len(upper_bounds) - 1:
            raise ValueError("Exactly the final LVR band must be open-ended.")
        if bounded_edges != sorted(set(bounded_edges)):
            raise ValueError("LVR band upper bounds must be strictly increasing.")

        if len({scenario.name for scenario in self.scenarios}) != len(self.scenarios):
            raise ValueError("Credit-loss scenario names must be unique.")
        if not math.isclose(
            math.fsum(scenario.weight for scenario in self.scenarios),
            1,
            rel_tol=0,
            abs_tol=SCENARIO_WEIGHT_ABS_TOLERANCE,
        ):
            raise ValueError("Credit-loss scenario weights must sum to one within 1e-12.")
        for scenario in self.scenarios:
            assumption_names = {entry.band_name for entry in scenario.assumptions}
            if len(scenario.assumptions) != len(names) or assumption_names != set(names):
                raise ValueError(f"Scenario {scenario.name!r} must specify each LVR band once.")
        return self

    def _band_names(self) -> tuple[str, ...]:
        return (*(band.name for band in self.bands), self.unknown_band_name)

    def _band_indices(self, lvr: ArrayLike) -> np.ndarray:
        raw = np.asarray(lvr)
        if raw.dtype == object:
            raw = np.where(pd.isna(raw), np.nan, raw)
        values = _numeric_array("LVR", raw)
        if values.ndim != 1 or np.isinf(values).any() or np.any(values < 0):
            raise ValueError("LVR must be a one-dimensional vector of non-negative ratios.")
        edges = [band.upper_lvr for band in self.bands[:-1]]
        # searchsorted(left) includes each finite edge in the band below it.
        indices = np.searchsorted(edges, values, side="left")
        indices[np.isnan(values)] = len(self.bands)
        return indices

    def assign_bands(self, lvr: ArrayLike) -> np.ndarray:
        """Map origination LVR ratios to right-closed bands, with an explicit missing band."""
        return np.asarray(self._band_names())[self._band_indices(lvr)]

    def estimate(
        self, principal_nzd: ArrayLike, lvr: ArrayLike | None = None
    ) -> CreditLossEstimate:
        """Estimate finite annual NZD losses by facility using joint scenarios.

        EAD may differ from principal. The illustrative multiplier basis gives
        EAD and loss of zero at zero principal.
        """
        # Revalidate at the modelling boundary, including caller-supplied Pydantic instances.
        profile = type(self).model_validate(self)
        principal = _numeric_array("Principal", principal_nzd)
        if principal.ndim != 1:
            raise ValueError("Principal must be a one-dimensional vector.")
        _require_range("Principal", principal)
        indices = (
            np.full(principal.shape, len(profile.bands), dtype=int)
            if lvr is None
            else profile._band_indices(lvr)
        )
        if indices.shape != principal.shape:
            raise ValueError("LVR and principal must have the same shape.")

        names = profile._band_names()
        evidence = []
        total_loss = np.zeros_like(principal)
        with np.errstate(over="ignore"):
            for scenario in profile.scenarios:
                assumptions = {entry.band_name: entry for entry in scenario.assumptions}
                pd_values, lgd, ead_multipliers = np.array(
                    [
                        (
                            assumptions[name].pd,
                            assumptions[name].lgd,
                            assumptions[name].ead_multiplier,
                        )
                        for name in names
                    ]
                )[indices].T
                ead = principal * ead_multipliers
                total_loss += scenario.weight * annual_expected_loss(pd_values, lgd, ead)
                evidence.append(
                    ScenarioLossEstimate(scenario.name, scenario.weight, pd_values, lgd, ead)
                )
        _require_range("Scenario-weighted loss", total_loss)
        return CreditLossEstimate(
            principal.copy(), np.asarray(names)[indices], total_loss, tuple(evidence)
        )


def _numeric_array(name: str, values: ArrayLike) -> np.ndarray:
    try:
        raw = np.asarray(values)
        if raw.dtype.kind in "cmM" or (
            raw.dtype == object
            and any(
                isinstance(value, (complex, np.complexfloating, np.datetime64, np.timedelta64))
                for value in raw.flat
            )
        ):
            raise TypeError("Complex or temporal value.")
        return np.asarray(raw, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be real numeric values.") from exc


def _require_range(name: str, values: np.ndarray, upper: float = np.inf) -> None:
    if not np.isfinite(values).all() or np.any(values < 0) or np.any(values > upper):
        bound = f"between zero and {upper:g}" if math.isfinite(upper) else "non-negative"
        raise ValueError(f"{name} must be finite and {bound}.")


def annual_expected_loss(
    pd: ArrayLike, lgd: ArrayLike, ead_nzd: ArrayLike
) -> np.ndarray | np.float64:
    """Compute PD × LGD × EAD from scalars or aligned facility vectors.

    PD is conditional on booking and covers the next 12 months.
    LGD and EAD are conditional on default. EAD is an independent NZD balance.
    """
    pd_values = _numeric_array("PD", pd)
    lgd_values = _numeric_array("LGD", lgd)
    ead = _numeric_array("EAD", ead_nzd)
    if pd_values.ndim > 1 or pd_values.shape != lgd_values.shape or pd_values.shape != ead.shape:
        raise ValueError("PD, LGD and EAD must be scalars or aligned one-dimensional vectors.")
    _require_range("PD", pd_values, 1)
    _require_range("LGD", lgd_values, 1)
    _require_range("EAD", ead)
    return pd_values * lgd_values * ead


def illustrative_profile() -> CreditLossProfile:
    """Return the reproducible Phase 4b profile, with invented assumptions."""
    bands = (
        CreditRiskBand(name="lvr_le_60", upper_lvr=0.6),
        CreditRiskBand(name="lvr_gt_60_le_80", upper_lvr=0.8),
        CreditRiskBand(name="lvr_gt_80_le_90", upper_lvr=0.9),
        CreditRiskBand(name="lvr_gt_90"),
    )
    names = (*(band.name for band in bands), "unknown")
    scenarios = (
        ("base", 0.8, 0.97, (0.006, 0.010, 0.016, 0.024, 0.024), (0.20, 0.25, 0.35, 0.45, 0.45)),
        (
            "collateral_stress",
            0.2,
            1.0,
            (0.009, 0.015, 0.024, 0.036, 0.036),
            (0.30, 0.35, 0.45, 0.55, 0.55),
        ),
    )
    return CreditLossProfile(
        version="illustrative-mortgage-v1",
        effective_date=date(2026, 11, 22),
        bands=bands,
        scenarios=tuple(
            CreditLossScenario(
                name=scenario,
                weight=weight,
                assumptions=tuple(
                    CreditLossParameters(
                        band_name=band, pd=pd_value, lgd=lgd, ead_multiplier=ead_multiplier
                    )
                    for band, pd_value, lgd in zip(names, pd_values, lgd_values)
                ),
            )
            for scenario, weight, ead_multiplier, pd_values, lgd_values in scenarios
        ),
        review_note="Invented for numerical checks and sensitivity analysis. Empirical review pending.",
    )

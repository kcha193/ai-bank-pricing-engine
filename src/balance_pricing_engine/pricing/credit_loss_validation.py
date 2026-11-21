"""Offline diagnostics for scored, booked mortgage facilities and observed outcomes."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from statistics import stdev
from typing import Annotated, Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sklearn.metrics import brier_score_loss, roc_auc_score

NonEmptyText = Annotated[str, Field(min_length=1)]
Probability = Annotated[float, Field(ge=0, le=1)]
NonNegativeMoney = Annotated[float, Field(ge=0)]
Metric = Optional[Union[float, int]]


class CreditLossOutcome(BaseModel):
    """One booked facility at one annual prediction horizon.

    An unknown 12-month default outcome is represented by None. An observed
    default may be present even if its workout remains open. A resolved workout
    needs observed EAD and both cash-flow totals for realised loss comparison.
    Record an observed zero explicitly. Cash flows must be reconciled upstream
    on a common valuation and discount basis. For scenario mixtures, supply
    annual ECL from the joint scenario products, and conditional PD/LGD/EAD
    diagnostics on their separately documented bases.
    """

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        str_strip_whitespace=True,
    )

    facility_id: NonEmptyText
    vintage: NonEmptyText
    lvr_band: NonEmptyText
    segment: NonEmptyText
    sample: NonEmptyText = "evaluation"
    booked_principal_nzd: NonNegativeMoney
    predicted_pd: Probability
    predicted_lgd: Probability
    predicted_ead_nzd: NonNegativeMoney
    predicted_annual_loss_nzd: NonNegativeMoney
    defaulted_12m: Optional[StrictBool] = None
    workout_resolved: StrictBool = False
    balance_at_default_nzd: Optional[NonNegativeMoney] = None
    recovered_amount_nzd: Optional[NonNegativeMoney] = None
    workout_cost_nzd: Optional[NonNegativeMoney] = None
    stressed_annual_loss_nzd: Optional[NonNegativeMoney] = None

    @model_validator(mode="after")
    def validate_outcome(self) -> CreditLossOutcome:
        if self.defaulted_12m is not True and (
            self.workout_resolved
            or self.balance_at_default_nzd is not None
            or self.recovered_amount_nzd is not None
            or self.workout_cost_nzd is not None
        ):
            raise ValueError(
                "Default exposure and workout observations require an observed default."
            )
        return self


def _validated_population(outcomes: Iterable[CreditLossOutcome]) -> list[CreditLossOutcome]:
    rows = [CreditLossOutcome.model_validate(row) for row in outcomes]
    if len({row.facility_id for row in rows}) != len(rows):
        raise ValueError("Each validation population must have a unique facility_id per row.")
    return rows


def _group(rows: Sequence[CreditLossOutcome], field: str) -> dict[str, list[CreditLossOutcome]]:
    groups: dict[str, list[CreditLossOutcome]] = defaultdict(list)
    for row in rows:
        groups[getattr(row, field)].append(row)
    return dict(sorted(groups.items()))


def _sum(rows: Sequence[CreditLossOutcome], field: str) -> float:
    return math.fsum(getattr(row, field) for row in rows)


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    ratio = numerator / denominator
    if not math.isfinite(ratio):
        raise ValueError("Validation metric exceeds the finite numeric range.")
    return ratio


def _change(evaluation: float | None, reference: float | None) -> float | None:
    if evaluation is None or reference is None:
        return None
    return math.fsum((evaluation, -reference))


def _wilson_95(defaults: int, n: int) -> list[float] | None:
    """95% Wilson score interval for a binomial default proportion."""
    if not n:
        return None
    z = 1.959963984540054
    observed_rate = defaults / n
    denominator = 1 + z**2 / n
    centre = (observed_rate + z**2 / (2 * n)) / denominator
    half_width = z * math.sqrt(observed_rate * (1 - observed_rate) / n + z**2 / (4 * n**2))
    half_width /= denominator
    return [max(0.0, centre - half_width), min(1.0, centre + half_width)]


def _pd_metrics(rows: Sequence[CreditLossOutcome]) -> dict[str, Any]:
    observed = [row for row in rows if row.defaulted_12m is not None]
    labels = [int(row.defaulted_12m) for row in observed]
    predicted = [row.predicted_pd for row in observed]
    n = len(observed)
    defaults = sum(labels)
    expected_defaults = math.fsum(predicted)
    return {
        "observed_facilities": n,
        "unobserved_facilities": len(rows) - n,
        "observed_principal_nzd": _sum(observed, "booked_principal_nzd"),
        "observed_defaults": defaults,
        "expected_defaults": expected_defaults,
        "observed_default_rate": _ratio(defaults, n),
        "predicted_default_rate": _ratio(expected_defaults, n),
        "observed_rate_wilson_95": _wilson_95(defaults, n),
        "brier_score": float(brier_score_loss(labels, predicted)) if n else None,
        "roc_auc": float(roc_auc_score(labels, predicted)) if 0 < defaults < n else None,
    }


def _realised_loss(row: CreditLossOutcome) -> float:
    return math.fsum((row.balance_at_default_nzd, -row.recovered_amount_nzd, row.workout_cost_nzd))


def _lgd_metrics(
    defaults: Sequence[CreditLossOutcome], complete: Sequence[CreditLossOutcome]
) -> dict[str, Metric]:
    resolved = [row for row in defaults if row.workout_resolved]
    # Zero exposure has no defined fractional severity. Preserve its loss in the ECL report.
    severity = [row for row in complete if row.balance_at_default_nzd > 0]
    ead = _sum(severity, "balance_at_default_nzd")
    loss = math.fsum(_realised_loss(row) for row in severity)
    predicted_loss_at_actual_ead = math.fsum(
        row.predicted_lgd * row.balance_at_default_nzd for row in severity
    )
    observed_lgd = _ratio(loss, ead)
    predicted_lgd = _ratio(predicted_loss_at_actual_ead, ead)
    severity_errors = [
        row.predicted_lgd - _ratio(_realised_loss(row), row.balance_at_default_nzd)
        for row in severity
    ]
    return {
        "resolved_defaults": len(resolved),
        "unresolved_defaults": len(defaults) - len(resolved),
        "resolved_missing_ead": sum(row.balance_at_default_nzd is None for row in resolved),
        "resolved_missing_recoveries": sum(row.recovered_amount_nzd is None for row in resolved),
        "resolved_missing_workout_costs": sum(row.workout_cost_nzd is None for row in resolved),
        "resolved_missing_cash_flows": sum(
            row.recovered_amount_nzd is None or row.workout_cost_nzd is None for row in resolved
        ),
        "resolved_zero_ead": len(complete) - len(severity),
        "severity_facilities": len(severity),
        "severity_ead_nzd": ead,
        "recoveries_nzd": _sum(severity, "recovered_amount_nzd"),
        "workout_costs_nzd": _sum(severity, "workout_cost_nzd"),
        "net_loss_nzd": loss,
        "observed_lgd": observed_lgd,
        "predicted_lgd": predicted_lgd,
        "severity_bias": _change(predicted_lgd, observed_lgd),
        "mean_severity_error_standard_error": (
            stdev(severity_errors) / math.sqrt(len(severity)) if len(severity) > 1 else None
        ),
    }


def _ead_metrics(defaults: Sequence[CreditLossOutcome]) -> dict[str, Metric]:
    matched = [row for row in defaults if row.balance_at_default_nzd is not None]
    booked = _sum(matched, "booked_principal_nzd") if matched else None
    observed = _sum(matched, "balance_at_default_nzd") if matched else None
    predicted = _sum(matched, "predicted_ead_nzd") if matched else None
    return {
        "defaults_with_ead": len(matched),
        "defaults_missing_ead": len(defaults) - len(matched),
        "booked_principal_nzd": booked,
        "observed_ead_nzd": observed,
        "predicted_ead_nzd": predicted,
        "observed_minus_booked_nzd": _change(observed, booked),
        "predicted_minus_observed_nzd": _change(predicted, observed),
        "mean_absolute_exposure_error_nzd": _ratio(
            math.fsum(abs(row.predicted_ead_nzd - row.balance_at_default_nzd) for row in matched),
            len(matched),
        ),
    }


def _ecl_metrics(
    rows: Sequence[CreditLossOutcome], complete: Sequence[CreditLossOutcome]
) -> dict[str, Metric]:
    matched = [row for row in rows if row.defaulted_12m is False] + list(complete)
    predicted = _sum(matched, "predicted_annual_loss_nzd") if matched else None
    realised = math.fsum(_realised_loss(row) for row in complete) if matched else None
    return {
        "matched_facilities": len(matched),
        "unmatched_facilities": len(rows) - len(matched),
        "matched_principal_nzd": _sum(matched, "booked_principal_nzd"),
        "predicted_loss_nzd": predicted,
        "realised_loss_nzd": realised,
        "predicted_minus_realised_nzd": _change(predicted, realised),
    }


def _stress_metrics(rows: Sequence[CreditLossOutcome]) -> dict[str, Metric]:
    paired = [row for row in rows if row.stressed_annual_loss_nzd is not None]
    baseline = _sum(paired, "predicted_annual_loss_nzd") if paired else None
    stress = _sum(paired, "stressed_annual_loss_nzd") if paired else None
    return {
        "matched_facilities": len(paired),
        "matched_principal_nzd": _sum(paired, "booked_principal_nzd"),
        "baseline_loss_nzd": baseline,
        "stressed_loss_nzd": stress,
        "loss_change_nzd": _change(stress, baseline),
    }


def _metrics(rows: Sequence[CreditLossOutcome]) -> dict[str, Any]:
    defaults = [row for row in rows if row.defaulted_12m is True]
    complete = [
        row
        for row in defaults
        if row.workout_resolved
        and row.balance_at_default_nzd is not None
        and row.recovered_amount_nzd is not None
        and row.workout_cost_nzd is not None
    ]
    return {
        "pd": _pd_metrics(rows),
        "lgd": _lgd_metrics(defaults, complete),
        "ead": _ead_metrics(defaults),
        "ecl": _ecl_metrics(rows, complete),
        "stress": _stress_metrics(rows),
    }


def assess_credit_loss(outcomes: Iterable[CreditLossOutcome]) -> dict[str, Any]:
    """Report observed vs predicted risk and loss for matched facilities.

    Supply already scored, split, horizon-aligned facilities. Group by sample
    and vintage to inspect a caller-assigned holdout without pooling it with
    development data. Estimation, approval criteria and sample selection are
    outside this function. A complete-case comparison is descriptive and can be
    biased when outcome completeness varies by risk.
    """
    rows = _validated_population(outcomes)
    return {
        "overall": _metrics(rows),
        **{
            f"by_{field}": {
                group: _metrics(subset) for group, subset in _group(rows, field).items()
            }
            for field in ("lvr_band", "vintage", "segment", "sample")
        },
        "by_sample_and_vintage": {
            sample: {
                vintage: _metrics(vintage_rows)
                for vintage, vintage_rows in _group(sample_rows, "vintage").items()
            }
            for sample, sample_rows in _group(rows, "sample").items()
        },
    }


def compare_population_mix(
    reference: Iterable[CreditLossOutcome], evaluation: Iterable[CreditLossOutcome]
) -> dict[str, Any]:
    """Report evaluation minus reference shares, without drift thresholds."""
    reference_rows = _validated_population(reference)
    evaluation_rows = _validated_population(evaluation)
    reference_principal = _sum(reference_rows, "booked_principal_nzd")
    evaluation_principal = _sum(evaluation_rows, "booked_principal_nzd")
    mixtures: dict[str, Any] = {}
    for field in ("lvr_band", "segment"):
        reference_groups = _group(reference_rows, field)
        evaluation_groups = _group(evaluation_rows, field)
        categories = {}
        for name in sorted(reference_groups.keys() | evaluation_groups.keys()):
            before = reference_groups.get(name, [])
            after = evaluation_groups.get(name, [])
            reference_count_share = _ratio(len(before), len(reference_rows))
            evaluation_count_share = _ratio(len(after), len(evaluation_rows))
            reference_principal_share = _ratio(
                _sum(before, "booked_principal_nzd"), reference_principal
            )
            evaluation_principal_share = _ratio(
                _sum(after, "booked_principal_nzd"), evaluation_principal
            )
            categories[name] = {
                "reference_facilities": len(before),
                "evaluation_facilities": len(after),
                "reference_count_share": reference_count_share,
                "evaluation_count_share": evaluation_count_share,
                "count_share_change": _change(evaluation_count_share, reference_count_share),
                "reference_principal_share": reference_principal_share,
                "evaluation_principal_share": evaluation_principal_share,
                "principal_share_change": _change(
                    evaluation_principal_share, reference_principal_share
                ),
            }
        mixtures[f"by_{field}"] = categories
    return mixtures

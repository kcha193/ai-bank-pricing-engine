"""Independent numerical checks for synthetic annual PD/LGD/EAD."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from balance_pricing_engine.pricing.credit_loss import (
    CreditLossProfile,
    annual_expected_loss,
    illustrative_profile,
)


@pytest.mark.parametrize(
    ("pd_value", "lgd", "ead", "expected"),
    [(0, 0.3, 100, 0), (0.1, 0, 100, 0), (0.1, 0.3, 0, 0), (1, 1, 100, 100)],
)
def test_annual_loss_limits(pd_value, lgd, ead, expected):
    assert annual_expected_loss(pd_value, lgd, ead) == expected


@given(
    pd_value=st.floats(0, 1),
    lgd=st.floats(0, 1),
    ead=st.floats(0, 1e9),
    multiplier=st.floats(0, 10),
)
def test_loss_scales_with_ead(pd_value, lgd, ead, multiplier):
    losses = annual_expected_loss([pd_value] * 2, [lgd] * 2, [ead, ead * multiplier])
    assert losses[0] == pytest.approx(pd_value * lgd * ead)
    assert losses[1] == pytest.approx(losses[0] * multiplier)


@pytest.mark.parametrize(
    "inputs",
    [
        (-0.1, 0.3, 100),
        (1.1, 0.3, 100),
        (0.1, -0.3, 100),
        (0.1, 1.3, 100),
        (0.1, 0.3, -100),
        (np.nan, 0.3, 100),
        (0.1, np.inf, 100),
        (0.1, 0.3, np.inf),
        (0.1, 0.3, np.nan),
        (0.1, 0.3, None),
        (0.1, 0.3, 100j),
        ([0.1], [0.3], np.array([np.complex128(100j)], dtype=object)),
        (0.1, 0.3, np.datetime64("2026-11-22")),
        ([0.1], [0.3, 0.5], [100, 200]),
        ([[0.1]], [[0.3]], [[100]]),
    ],
)
def test_invalid_primitive_inputs_fail(inputs):
    with pytest.raises(ValueError):
        annual_expected_loss(*inputs)


def test_lvr_boundaries_and_unknown():
    lvr = [
        0,
        0.6,
        np.nextafter(0.6, 1),
        0.8,
        np.nextafter(0.8, 1),
        0.9,
        0.91,
        1.1,
        None,
        np.nan,
        pd.NA,
    ]
    result = illustrative_profile().estimate([100] * len(lvr), lvr)
    assert list(result.band_names) == [
        "lvr_le_60",
        "lvr_le_60",
        "lvr_gt_60_le_80",
        "lvr_gt_60_le_80",
        "lvr_gt_80_le_90",
        "lvr_gt_80_le_90",
        "lvr_gt_90",
        "lvr_gt_90",
        "unknown",
        "unknown",
        "unknown",
    ]


@pytest.mark.parametrize(
    ("principal", "lvr"),
    [
        ([-1], None),
        ([np.nan], None),
        ([np.inf], None),
        ([1], [np.inf]),
        ([1], [-np.inf]),
        ([1], [-0.1]),
        ([1], [0.5, 0.7]),
        (1, [0.8]),
        ([1], 0.8),
        ([1], ["not an LVR"]),
    ],
)
def test_profile_requires_valid_aligned_principal_and_lvr(principal, lvr):
    with pytest.raises(ValueError):
        illustrative_profile().estimate(principal, lvr)


def test_profile_calculates_scenario_weighted_loss_and_retains_ead():
    raw = illustrative_profile().model_dump()
    names = [band["name"] for band in raw["bands"]] + [raw["unknown_band_name"]]
    raw["scenarios"] = [
        {
            "name": name,
            "weight": weight,
            "assumptions": [
                {"band_name": band, "pd": pd_value, "lgd": lgd, "ead_multiplier": ead}
                for band in names
            ],
        }
        for name, weight, pd_value, lgd, ead in [
            ("first", 0.8, 0.1, 0.2, 0.5),
            ("second", 0.2, 0.3, 0.4, 1.2),
        ]
    ]
    profile = CreditLossProfile.model_validate(raw)
    estimate = profile.estimate([0, 500_000])
    first, second = estimate.scenarios
    assert first.name == "first"
    assert second.name == "second"
    assert first.weight + second.weight == 1
    assert first.pd.tolist() == [0.1, 0.1]
    assert first.lgd.tolist() == [0.2, 0.2]
    assert first.ead_nzd.tolist() == [0, 250_000]
    assert second.ead_nzd.tolist() == [0, 600_000]
    assert estimate.principal_nzd.tolist() == [0, 500_000]
    assert estimate.band_names.tolist() == ["unknown", "unknown"]
    assert estimate.annual_expected_loss_nzd == pytest.approx(
        [0, 0.8 * 0.1 * 0.2 * 250_000 + 0.2 * 0.3 * 0.4 * 600_000]
    )


@pytest.mark.parametrize(
    "weights", [(-0.2, 1.2), (np.nan, 1), (np.inf, 0), (0.4, 0.4), (0.8, 0.200000000002)]
)
def test_profile_rejects_invalid_scenario_weights(weights):
    raw = illustrative_profile().model_dump()
    for scenario, weight in zip(raw["scenarios"], weights):
        scenario["weight"] = weight
    with pytest.raises(ValidationError):
        CreditLossProfile.model_validate(raw)


def test_scenario_weight_tolerance_is_absolute():
    raw = illustrative_profile().model_dump()
    raw["scenarios"][1]["weight"] += 1e-13
    CreditLossProfile.model_validate(raw)


@pytest.mark.parametrize(
    "invalid_case",
    [
        "duplicate_band_name",
        "duplicate_edge",
        "nonmonotone_edge",
        "empty_final_band",
        "early_open_band",
        "unknown_collides",
        "duplicate_scenario",
        "missing_assumption",
        "duplicate_assumption",
        "unexpected_assumption",
        "bad_version",
        "negative_ead",
        "negative_pd",
        "excess_pd",
        "negative_lgd",
        "excess_lgd",
        "nonfinite_ead",
        "invalid_classification",
        "invalid_status",
        "claims_approval",
    ],
)
def test_invalid_profiles_fail(invalid_case):
    raw = illustrative_profile().model_dump()
    first, second, *_, last = raw["bands"]
    first_scenario, second_scenario = raw["scenarios"]
    first_assumption, second_assumption, *_ = first_scenario["assumptions"]
    target, key, value = {
        "duplicate_band_name": (second, "name", first["name"]),
        "duplicate_edge": (second, "upper_lvr", first["upper_lvr"]),
        "nonmonotone_edge": (second, "upper_lvr", 0.5),
        "empty_final_band": (last, "upper_lvr", 1.0),
        "early_open_band": (first, "upper_lvr", None),
        "unknown_collides": (raw, "unknown_band_name", first["name"]),
        "duplicate_scenario": (second_scenario, "name", first_scenario["name"]),
        "missing_assumption": (first_scenario, "assumptions", first_scenario["assumptions"][1:]),
        "duplicate_assumption": (second_assumption, "band_name", first_assumption["band_name"]),
        "unexpected_assumption": (second_assumption, "band_name", "extra"),
        "bad_version": (raw, "version", " "),
        "negative_ead": (first_assumption, "ead_multiplier", -1),
        "negative_pd": (first_assumption, "pd", -0.5),
        "excess_pd": (first_assumption, "pd", 1.5),
        "negative_lgd": (first_assumption, "lgd", -0.5),
        "excess_lgd": (first_assumption, "lgd", 1.5),
        "nonfinite_ead": (first_assumption, "ead_multiplier", np.inf),
        "invalid_classification": (raw, "evidence_classification", "empirical"),
        "invalid_status": (raw, "validation_status", "validated"),
        "claims_approval": (raw, "approved", True),
    }[invalid_case]
    target[key] = value
    with pytest.raises(ValidationError):
        CreditLossProfile.model_validate(raw)


def test_profile_round_trip_is_deterministic_and_immutable():
    profile = illustrative_profile()
    restored = CreditLossProfile.model_validate_json(profile.model_dump_json())
    assert restored == profile == illustrative_profile()
    assert restored.effective_date == date(2026, 11, 22)
    assert restored.pd_horizon_months == 12
    with pytest.raises(ValidationError):
        profile.version = "changed"
    with pytest.raises(ValidationError):
        profile.model_copy(update={"validation_status": "empirically_validated"})
    with pytest.raises(ValidationError):
        profile.scenarios[0].assumptions[0].pd = -1
    assert restored.estimate([], []).annual_expected_loss_nzd.size == 0
    assert restored.estimate([1], [0.5]).annual_expected_loss_nzd == pytest.approx(
        profile.estimate([1], [0.5]).annual_expected_loss_nzd
    )


def test_ead_overflow_is_rejected():
    profile = illustrative_profile()
    raw = profile.model_dump()
    raw["scenarios"][0]["assumptions"][0]["ead_multiplier"] = 2
    profile = CreditLossProfile.model_validate(raw)
    with pytest.raises(ValueError, match="EAD"):
        profile.estimate([np.finfo(float).max], [0.5])


def test_configured_single_band_and_scenario():
    profile = CreditLossProfile(
        version="catch-all-v1",
        effective_date=date(2026, 11, 22),
        bands=[{"name": "all_known"}],
        unknown_band_name="missing",
        scenarios=[
            {
                "name": "only",
                "weight": 1,
                "assumptions": [
                    {"band_name": "all_known", "pd": 0.01, "lgd": 0.3, "ead_multiplier": 0.7},
                    {"band_name": "missing", "pd": 0.02, "lgd": 0.5, "ead_multiplier": 1},
                ],
            }
        ],
        review_note="Synthetic fixture.",
    )
    lvr = pd.Series([0.5, None], dtype="Float64")
    assert profile.assign_bands(lvr).tolist() == ["all_known", "missing"]
    estimate = profile.estimate([200_000, 100_000], lvr)
    assert estimate.annual_expected_loss_nzd == pytest.approx([420, 1000])

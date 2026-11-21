"""TEST-002: finite-input and domain validation (OPT-003 regression).

``test_financial_input_validation.py`` checks a hand-written list of fields. This module
*discovers* every numeric leaf of the economic sections in the shipped configs (joint,
market-relative, segmented and multi-period), so a field added later without finite-value
validation fails here without anyone remembering to list it. Each invalid value is checked
on four routes: direct construction, YAML load, copy-with-update and scenario-entry
revalidation of an instance edited in place.

Legitimate boundary values (zero elasticity, zero penalty, zero and full runoff, equal grid
endpoints, funding ratio 0 and 1) must stay valid: validation must not be over-restrictive.
"""

from __future__ import annotations

import copy
import math
import tempfile
from pathlib import Path

import pytest
import yaml
from economic_strategies import budget, volumes
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.scenarios.cohort_accounting import BookedCohort, survival_factor
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

pytestmark = [
    pytest.mark.economic_invariants,
    pytest.mark.filterwarnings("ignore::UserWarning:balance_pricing_engine.config"),
    # In-place edits of int fields to NaN are the point of the scenario-entry route.
    pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning"),
]

ECONOMIC_SECTIONS = (
    "simulation",
    "market",
    "products",
    "pricing_strategy",
    "segmented_pricing",
    "balance_sheet",
    "response",
    "multiperiod",
)
_MULTIPERIOD = {
    "n_periods": 3,
    "loan_runoff_rate": 0.3,
    "deposit_runoff_rate": 0.5,
    "discount_rate": 0.08,
    "market_cash_rate_drift_bps": 0.0,
    "competitor_loan_rate_drift_bps": 0.0,
    "competitor_deposit_rate_drift_bps": 0.0,
    "reinvestment_spread_bps": 0.0,
    "loan_tenor_years": 5.0,
}


def _variants() -> dict[str, dict]:
    base = yaml.safe_load(Path("configs/base.yaml").read_text())
    base["multiperiod"] = dict(_MULTIPERIOD)
    base["balance_sheet"].update(average_loan_tenor_years=5.0, min_return_on_equity=0.1)
    joint = copy.deepcopy(base)
    del joint["pricing_strategy"]
    joint["products"] = {
        "loan_rate_grid": {"start": 0.045, "stop": 0.065, "step": 0.005},
        "deposit_rate_grid": {"start": 0.030, "stop": 0.045, "step": 0.005},
    }
    segmented = yaml.safe_load(Path("configs/segmented_pricing.yaml").read_text())
    return {"market_relative": base, "joint": joint, "segmented": segmented}


VARIANTS = _variants()


def _numeric_leaves(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _numeric_leaves(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _numeric_leaves(value, (*path, index))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield path


LEAVES = sorted(
    {
        (name, path)
        for name, raw in VARIANTS.items()
        for path in _numeric_leaves(raw)
        if path[0] in ECONOMIC_SECTIONS
    },
    key=repr,
)
NONFINITE = (float("nan"), float("inf"), float("-inf"))
_BAD = st.sampled_from(NONFINITE)


def _set(node, path, value):
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _set_attribute(obj, path, value):
    for key in path[:-1]:
        obj = obj[key] if isinstance(key, int) else getattr(obj, key)
    if isinstance(path[-1], int):
        obj[path[-1]] = value
    else:
        object.__setattr__(obj, path[-1], value)


def test_leaf_discovery_covers_every_economic_section():
    sections = {path[0] for _, path in LEAVES}
    assert sections == set(ECONOMIC_SECTIONS)
    assert len(LEAVES) >= 60


def test_v01_every_economic_number_rejects_nan_and_infinity_on_every_route():
    # Exhaust the bounded schema × non-finite value matrix so no leaf depends on sampling.
    for variant, path in LEAVES:
        for bad in NONFINITE:
            _assert_rejected_on_every_route(variant, path, bad)


def _assert_rejected_on_every_route(variant, path, bad):
    raw = copy.deepcopy(VARIANTS[variant])
    valid = EngineConfig.model_validate(copy.deepcopy(raw))
    _set(raw, path, bad)

    with pytest.raises(ValidationError):  # direct construction
        EngineConfig.model_validate(copy.deepcopy(raw))

    with tempfile.TemporaryDirectory() as directory:  # YAML load
        source = Path(directory) / "config.yaml"
        source.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValidationError):
            load_config(source)

    with pytest.raises(ValidationError):  # copy-with-update of the whole section
        valid.model_copy(update={path[0]: raw[path[0]]})

    # Scenario entry: an instance edited in place must be revalidated before pricing.
    edited = valid.model_copy()
    _set_attribute(edited, path, bad)
    with pytest.raises(ValidationError):
        run_baseline_diagnostics(edited)


@given(
    variant=st.sampled_from(sorted(VARIANTS)),
    loan_own=st.floats(1e-9, 1e4),
    deposit_own=st.floats(1e-9, 1e4),
    which=st.sampled_from(["loan_rate_elasticity", "loan_competitor_gap_elasticity",
                           "deposit_rate_elasticity", "deposit_competitor_gap_elasticity"]),
)
@budget(60)
def test_v02_wrong_sign_elasticity_of_any_magnitude_is_rejected(
    variant, loan_own, deposit_own, which
):
    raw = copy.deepcopy(VARIANTS[variant])
    raw["response"][which] = loan_own if which.startswith("loan") else -deposit_own
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(raw)


@st.composite
def _invalid_domain_edit(draw):
    """One out-of-domain edit: grid step <= 0, inverted grid, ratio outside [0, 1],
    negative penalty or hurdle, runoff outside [0, 1], negative discount, too few periods."""
    kind = draw(st.sampled_from(["step", "inverted", "ratio", "penalty", "hurdle", "runoff",
                                 "discount", "periods", "customers"]))
    beyond = draw(st.floats(1e-9, 1e3))
    if kind == "step":
        return ("products", "loan_rate_grid", "step"), -draw(st.floats(0.0, 1.0))
    if kind == "inverted":
        return ("products", "loan_rate_grid", "stop"), 0.045 - beyond
    if kind == "ratio":
        return ("balance_sheet", "required_deposit_funding_ratio"), draw(
            st.sampled_from([-beyond, 1.0 + beyond])
        )
    if kind == "penalty":
        return ("balance_sheet", "funding_shortfall_penalty_rate"), -beyond
    if kind == "hurdle":
        return ("balance_sheet", "capital_hurdle_rate"), -beyond
    if kind == "runoff":
        field = draw(st.sampled_from(["loan_runoff_rate", "deposit_runoff_rate"]))
        return ("multiperiod", field), draw(st.sampled_from([-beyond, 1.0 + beyond]))
    if kind == "discount":
        return ("multiperiod", "discount_rate"), -beyond
    if kind == "periods":
        return ("multiperiod", "n_periods"), draw(st.integers(-5, 1))
    return ("simulation", "n_loan_customers"), draw(st.integers(-5, 0))


@given(edit=_invalid_domain_edit())
@budget(120)
def test_v03_out_of_domain_values_are_rejected(edit):
    path, value = edit
    raw = copy.deepcopy(VARIANTS["joint"])
    _set(raw, path, value)
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(raw)


@given(
    zero_elasticities=st.booleans(),
    zero_penalty=st.booleans(),
    ratio=st.sampled_from([0.0, 1.0]),
    loan_runoff=st.sampled_from([0.0, 1.0]),
    deposit_runoff=st.sampled_from([0.0, 1.0]),
    equal_endpoints=st.booleans(),
    zero_hurdle=st.booleans(),
)
@budget(60)
def test_v04_legitimate_boundary_values_stay_valid(
    zero_elasticities, zero_penalty, ratio, loan_runoff, deposit_runoff, equal_endpoints,
    zero_hurdle,
):
    raw = copy.deepcopy(VARIANTS["joint"])
    if zero_elasticities:
        for field in ("loan_rate_elasticity", "loan_competitor_gap_elasticity",
                      "deposit_rate_elasticity", "deposit_competitor_gap_elasticity"):
            raw["response"][field] = 0.0
    if zero_penalty:
        raw["balance_sheet"]["funding_shortfall_penalty_rate"] = 0.0
    if zero_hurdle:
        raw["balance_sheet"]["capital_hurdle_rate"] = 0.0
    raw["balance_sheet"]["required_deposit_funding_ratio"] = ratio
    raw["multiperiod"].update(loan_runoff_rate=loan_runoff, deposit_runoff_rate=deposit_runoff,
                              discount_rate=0.0, loan_tenor_years=None)
    if equal_endpoints:
        raw["products"]["loan_rate_grid"].update(start=0.05, stop=0.05)
    config = EngineConfig.model_validate(raw)
    assert config.balance_sheet.required_deposit_funding_ratio == ratio
    assert config.multiperiod.loan_runoff_rate == loan_runoff


@given(
    field=st.sampled_from(["original_loan_volume", "original_deposit_volume",
                           "booked_credit_loss", "loan_rwa_weight", "capital_hurdle_rate"]),
    bad=st.one_of(st.floats(-1e9, -1e-9), _BAD),
)
@budget(80)
def test_v05_booked_cohorts_reject_negative_or_nonfinite_balances(field, bad):
    values = dict(origination_period=0, loan_rate=0.06, deposit_rate=0.04,
                  original_loan_volume=1e6, original_deposit_volume=8e5,
                  booked_credit_loss=1e3, loan_rwa_weight=0.35, capital_hurdle_rate=0.12)
    values[field] = bad
    with pytest.raises(ValueError):
        BookedCohort(**values)


@given(runoff=st.one_of(st.floats(-1e3, -1e-12), st.floats(1.0 + 1e-12, 1e3), _BAD))
@budget(60)
def test_v06_runoff_outside_unit_interval_is_rejected_by_cohort_survival(runoff):
    with pytest.raises(ValueError):
        survival_factor(runoff, 1)


@given(volume=volumes, runoff=st.floats(0.0, 1.0), periods=st.integers(0, 50))
@budget(60)
def test_v07_valid_survival_inputs_give_a_finite_fraction(volume, runoff, periods):
    fraction = survival_factor(runoff, periods)
    assert math.isfinite(fraction) and 0.0 <= fraction <= 1.0
    assert math.isfinite(volume * fraction)

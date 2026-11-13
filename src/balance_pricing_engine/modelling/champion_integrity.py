"""Integrity and scoring-contract checks for governed ML champions (audit GOV-001 / SEC-007).

One validator serves both moments that matter:

- **Approval** (``ModelRegistry.approve_model`` / ``promote_champion``): the artifact is
  hashed, loaded from those same bytes, and checked. The hash, the ordered feature contract,
  the product and the probe evidence are then written to the registry record.
- **Resolution** (``ModelScoringAgent.resolve_response_fn``): the bytes are checked against
  the approved hash *before* they are deserialised, and the same checks run again before the
  model is allowed to drive pricing.

Checks, in order:

1. The artifact exists and, when an approved hash is supplied, its bytes match it.
2. The loaded object exposes a supported prediction interface: ``predict_probability``
   (the ``ResponseModel`` protocol) or ``predict_proba`` (sklearn).
3. The object declares its feature contract (``feature_names_in_``; ``n_features_in_`` when
   present) and it equals the approved ordered feature list. A model without verifiable
   feature names is rejected, because column order cannot be confirmed.
4. A deterministic synthetic probe, run through ``MLResponseFnAdapter`` exactly as pricing
   uses it, returns a 1-D array with one finite probability in [0, 1] per customer.
5. The probe's mean response moves in the economically sensible direction as the offered
   rate rises with all other inputs held fixed: non-increasing for loans, non-decreasing for
   deposits. A step counts as a violation only when it moves the wrong way by more than
   ``MONOTONE_TOLERANCE``, so a saturated model is not rejected for flat steps. Across the
   whole probe range the response must move in the right direction by more than the
   tolerance; a fully flat response means the direction cannot be established, so it fails.

Limitations: the probe checks the mean response over a fixed synthetic cohort at one fixed
competitor rate. It does not check calibration, per-customer monotonicity, the competitor-gap
elasticity, or behaviour outside the probe rate range. Passing it is a minimum bar for
pricing use, not a model validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from balance_pricing_engine.modelling.persistence import (
    load_model_from_bytes,
    sha256_hex,
    verify_artifact_bytes,
)

__all__ = [
    "INTEGRITY_SCHEMA_VERSION",
    "MONOTONE_TOLERANCE",
    "PROBE_SEED",
    "PROBE_N_CUSTOMERS",
    "PROBE_GRID",
    "SUPPORTED_PRODUCTS",
    "ChampionValidationError",
    "ChampionIntegrityEvidence",
    "validate_product",
    "validate_feature_contract",
    "validate_prediction_interface",
    "validate_artifact_features",
    "probe_response",
    "load_and_validate_artifact",
]

INTEGRITY_SCHEMA_VERSION = 1
SUPPORTED_PRODUCTS = ("loan", "deposit")

# Absolute tolerance on mean predicted probability; matches modelling.evaluation's
# check_monotonicity so the two monotonicity checks use one convention.
MONOTONE_TOLERANCE = 1e-6

PROBE_SEED = 20261113
PROBE_N_CUSTOMERS = 200
# (fixed competitor rate, increasing offered rates). The ranges span the configured NZ rate
# grids and every training fixture in the repository.
PROBE_GRID: dict[str, tuple[float, tuple[float, ...]]] = {
    "loan": (0.055, tuple(float(r) for r in np.round(np.linspace(0.03, 0.10, 15), 6))),
    "deposit": (0.045, tuple(float(r) for r in np.round(np.linspace(0.01, 0.08, 15), 6))),
}
_EXPECTED_DIRECTION = {"loan": "non_increasing", "deposit": "non_decreasing"}


class ChampionValidationError(ValueError):
    """An artifact failed an integrity or scoring-contract check."""


@dataclass(frozen=True)
class ChampionIntegrityEvidence:
    """Structured evidence that an artifact passed every check."""

    artifact_sha256: str
    product: str
    feature_names: tuple[str, ...]
    n_features: int
    prediction_interface: str
    probe: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "product": self.product,
            "feature_names": list(self.feature_names),
            "n_features": self.n_features,
            "prediction_interface": self.prediction_interface,
            "probe": dict(self.probe),
        }


def validate_product(product: Any) -> str:
    if product not in SUPPORTED_PRODUCTS:
        raise ChampionValidationError(
            f"product must be one of {list(SUPPORTED_PRODUCTS)}, got {product!r}"
        )
    return str(product)


def validate_feature_contract(feature_columns: Any) -> list[str]:
    """Return the declared feature_columns as a list, or raise if it is not a usable contract."""
    if not isinstance(feature_columns, (list, tuple)) or not feature_columns:
        raise ChampionValidationError(
            "feature_columns must be a non-empty ordered list of feature names"
        )
    names = list(feature_columns)
    if not all(isinstance(name, str) and name for name in names):
        raise ChampionValidationError("feature_columns must contain only non-empty strings")
    if len(set(names)) != len(names):
        raise ChampionValidationError("feature_columns must not contain duplicates")
    return names


def validate_prediction_interface(model: Any) -> str:
    if isinstance(model, type):
        raise ChampionValidationError(
            f"artifact is a class ({model.__name__}), not a fitted model; "
            "no supported prediction interface"
        )
    if callable(getattr(model, "predict_probability", None)):
        return "predict_probability"
    if callable(getattr(model, "predict_proba", None)):
        return "predict_proba"
    raise ChampionValidationError(
        f"artifact of type {type(model).__name__} has no supported prediction interface "
        "(predict_probability or predict_proba)"
    )


def validate_artifact_features(model: Any, feature_columns: list[str]) -> tuple[list[str], int]:
    """Check the artifact's own feature declaration against the approved ordered contract."""
    declared_count = getattr(model, "n_features_in_", None)
    if declared_count is not None and int(declared_count) != len(feature_columns):
        raise ChampionValidationError(
            f"feature count mismatch: artifact expects {int(declared_count)} features, "
            f"approved contract has {len(feature_columns)}"
        )
    declared_names = getattr(model, "feature_names_in_", None)
    if declared_names is None:
        raise ChampionValidationError(
            "artifact declares no feature names (feature_names_in_), so its feature order "
            "cannot be verified; retrain on a named DataFrame before approval"
        )
    names = [str(name) for name in list(declared_names)]
    if len(names) != len(feature_columns):
        raise ChampionValidationError(
            f"feature count mismatch: artifact declares {len(names)} feature names, "
            f"approved contract has {len(feature_columns)}"
        )
    if names != list(feature_columns):
        if set(names) == set(feature_columns):
            raise ChampionValidationError(
                "feature order mismatch: artifact feature order differs from the approved contract"
            )
        unexpected = sorted(set(names) - set(feature_columns))
        missing = sorted(set(feature_columns) - set(names))
        raise ChampionValidationError(
            f"feature name mismatch: unexpected={unexpected} missing={missing}"
        )
    return names, len(names)


def _probe_customers(product: str):
    from balance_pricing_engine.synthetic.customers import (
        generate_deposit_customers,
        generate_loan_customers,
    )

    # A private generator: the probe never touches the pricing run's random stream.
    rng = np.random.default_rng(PROBE_SEED)
    if product == "loan":
        return generate_loan_customers(PROBE_N_CUSTOMERS, rng)
    return generate_deposit_customers(PROBE_N_CUSTOMERS, rng)


def _checked_probabilities(raw: Any, n_rows: int, offered_rate: float) -> np.ndarray:
    values = np.asarray(raw)
    if values.shape != (n_rows,):
        raise ChampionValidationError(
            f"prediction shape {values.shape} at offered_rate={offered_rate}; "
            f"expected ({n_rows},)"
        )
    try:
        values = values.astype(float)
    except (TypeError, ValueError) as exc:
        raise ChampionValidationError(
            f"non-numeric predictions at offered_rate={offered_rate}"
        ) from exc
    if not np.all(np.isfinite(values)):
        raise ChampionValidationError(
            f"non-finite predictions (NaN or inf) at offered_rate={offered_rate}"
        )
    if np.any(values < 0.0) or np.any(values > 1.0):
        raise ChampionValidationError(
            f"predictions outside [0, 1] at offered_rate={offered_rate} "
            f"(min={values.min():.6g}, max={values.max():.6g})"
        )
    return values


def probe_response(model: Any, feature_columns: list[str], product: str) -> dict[str, Any]:
    """Run the deterministic probe through the pricing adapter and return its evidence."""
    from balance_pricing_engine.optimization.response_fn import MLResponseFnAdapter

    product = validate_product(product)
    customers = _probe_customers(product)
    adapter = MLResponseFnAdapter(model=model, feature_columns=list(feature_columns))
    competitor_rate, offered_rates = PROBE_GRID[product]
    means: list[float] = []
    for offered_rate in offered_rates:
        try:
            raw = adapter(customers, offered_rate, competitor_rate)
        except Exception as exc:  # noqa: BLE001 - any scoring failure fails validation loudly
            raise ChampionValidationError(
                f"prediction failed during probe at offered_rate={offered_rate}: "
                f"{type(exc).__name__}"
            ) from exc
        means.append(float(_checked_probabilities(raw, len(customers), offered_rate).mean()))

    steps = np.diff(np.asarray(means))
    direction = _EXPECTED_DIRECTION[product]
    if direction == "non_increasing":
        wrong_way = steps > MONOTONE_TOLERANCE
        overall = means[0] - means[-1]
    else:
        wrong_way = steps < -MONOTONE_TOLERANCE
        overall = means[-1] - means[0]
    evidence = {
        "status": "passed",
        "seed": PROBE_SEED,
        "n_customers": len(customers),
        "competitor_rate": competitor_rate,
        "offered_rates": list(offered_rates),
        "mean_probabilities": means,
        "direction": direction,
        "tolerance": MONOTONE_TOLERANCE,
    }
    if np.any(wrong_way):
        worst = int(np.argmax(np.abs(steps) * wrong_way))
        raise ChampionValidationError(
            f"economically inverted {product} response: mean probability moves the wrong way "
            f"between offered rates {offered_rates[worst]} and {offered_rates[worst + 1]} "
            f"({means[worst]:.6g} -> {means[worst + 1]:.6g}); expected {direction}"
        )
    if overall <= MONOTONE_TOLERANCE:
        raise ChampionValidationError(
            f"{product} rate-response direction could not be established: mean probability "
            f"changes by {overall:.3g} across offered rates {offered_rates[0]}-"
            f"{offered_rates[-1]} (tolerance {MONOTONE_TOLERANCE}); a flat response cannot "
            "be verified as economically sensible"
        )
    return evidence


def load_and_validate_artifact(
    artifact_path: str | Path,
    feature_columns: list[str],
    product: str,
    expected_sha256: str | None = None,
) -> tuple[Any, ChampionIntegrityEvidence]:
    """Read, verify, load and check an artifact. Return the loaded model and its evidence.

    The bytes are read once. When ``expected_sha256`` is given, they are verified before
    deserialisation, and the returned model is built from exactly those verified bytes.
    """
    product = validate_product(product)
    contract = validate_feature_contract(feature_columns)
    path = Path(artifact_path)
    if not path.is_file():
        raise ChampionValidationError(f"artifact is missing (artifact_path={str(path)!r})")
    data = path.read_bytes()
    digest = (
        verify_artifact_bytes(data, expected_sha256)
        if expected_sha256 is not None
        else sha256_hex(data)
    )
    try:
        model = load_model_from_bytes(data)
    except Exception as exc:  # noqa: BLE001 - corrupt/unloadable artifacts fail validation
        raise ChampionValidationError(
            f"artifact could not be loaded ({type(exc).__name__})"
        ) from exc
    interface = validate_prediction_interface(model)
    names, n_features = validate_artifact_features(model, contract)
    probe = probe_response(model, contract, product)
    return model, ChampionIntegrityEvidence(
        artifact_sha256=digest,
        product=product,
        feature_names=tuple(names),
        n_features=n_features,
        prediction_interface=interface,
        probe=probe,
    )

"""Approved-champion integrity and scoring-contract checks (audit GOV-001 / SEC-007).

Cases J1-J4c mirror ``docs/audits/2026-11-13-evidence/mlgov/challenge_j.py``. T1-T15 are the
additional Phase 1b regression cases. Every registry and artifact lives under ``tmp_path``.

"Force-approved" records below are written straight into the registry JSON with a *correct*
SHA-256 of the artifact. They model a registry that bypassed ``approve_model`` (hand-edited,
or written by an older tool), so each test reaches the specific resolution-time check it
names instead of stopping at the missing-metadata check.
"""

from __future__ import annotations

import hashlib
import json

import joblib
import numpy as np
import pytest

from balance_pricing_engine.modelling import champion_integrity
from balance_pricing_engine.modelling.agentic import (
    BootstrapEnsembleResponseModel,
    ModelRegistry,
    ModelScoringAgent,
)
from balance_pricing_engine.modelling.champion_integrity import (
    MONOTONE_TOLERANCE,
    ChampionValidationError,
    load_and_validate_artifact,
)
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.models import build_logistic_regression_model
from balance_pricing_engine.modelling.persistence import (
    ArtifactIntegrityError,
    artifact_sha256,
    load_verified_model,
    save_model,
)
from balance_pricing_engine.optimization.response_fn import (
    GovernanceError,
    MLResponseFnAdapter,
    SyntheticDepositResponseFn,
    SyntheticLoanResponseFn,
)
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng

LOAN_FALLBACK = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
DEPOSIT_FALLBACK = SyntheticDepositResponseFn(elasticity=80.0, competitor_gap_elasticity=60.0)


# --------------------------------------------------------------------------- fixtures


def _loan_dataset(seed: int = 123):
    rng = make_rng(seed)
    customers = generate_loan_customers(300, rng)
    return build_loan_response_dataset(
        customers,
        offered_rates=np.array([0.045, 0.055, 0.065, 0.075]),
        competitor_rate=0.06,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=2000,
    )


def _deposit_dataset(seed: int = 456):
    rng = make_rng(seed)
    customers = generate_deposit_customers(300, rng)
    return build_deposit_response_dataset(
        customers,
        offered_rates=np.array([0.03, 0.04, 0.05, 0.06]),
        competitor_rate=0.045,
        rate_elasticity=80.0,
        competitor_gap_elasticity=60.0,
        rng=rng,
        n_samples=2000,
    )


def _trained_loan_model():
    dataset = _loan_dataset()
    return build_logistic_regression_model(42).fit(
        dataset[LOAN_FEATURE_COLUMNS], dataset[LOAN_TARGET_COLUMN]
    )


def _trained_deposit_model():
    dataset = _deposit_dataset()
    return build_logistic_regression_model(42).fit(
        dataset[DEPOSIT_FEATURE_COLUMNS], dataset[DEPOSIT_TARGET_COLUMN]
    )


class StubResponseModel:
    """Picklable ``predict_proba`` stub whose response shape is chosen by ``mode``.

    It declares sklearn-style ``feature_names_in_`` / ``n_features_in_`` so that the feature
    contract check passes and the probe-level check under test is what decides the outcome.
    """

    def __init__(self, feature_names: list[str], mode: str):
        self.feature_names_in_ = np.asarray(feature_names, dtype=object)
        self.n_features_in_ = len(feature_names)
        self.mode = mode

    def predict_proba(self, frame):
        offered = frame["offered_rate"].to_numpy(dtype=float)
        if self.mode == "sane_loan":
            p = 1.0 / (1.0 + np.exp(50.0 * (offered - 0.06)))
        elif self.mode == "inverted_loan":
            p = 1.0 / (1.0 + np.exp(-50.0 * (offered - 0.06)))
        elif self.mode == "sane_deposit":
            p = 1.0 / (1.0 + np.exp(-50.0 * (offered - 0.045)))
        elif self.mode == "inverted_deposit":
            p = 1.0 / (1.0 + np.exp(50.0 * (offered - 0.045)))
        elif self.mode == "saturated_loan":
            # Flat at 1.0 / 0.0 over most of the probe range, decreasing in the middle.
            p = 1.0 / (1.0 + np.exp(np.clip(2_000.0 * (offered - 0.06), -700, 700)))
        elif self.mode == "flat":
            p = np.full(len(frame), 0.5)
        elif self.mode == "nan":
            p = np.full(len(frame), 0.5)
            p[0] = np.nan
        elif self.mode == "inf":
            p = np.full(len(frame), 0.5)
            p[0] = np.inf
        elif self.mode == "out_of_range":
            p = np.full(len(frame), 1.5)
        elif self.mode == "bad_shape":
            p = np.full(len(frame) - 1, 0.5)
        else:  # pragma: no cover - test misuse
            raise AssertionError(self.mode)
        return np.column_stack([1.0 - p, p])


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _force_approved(
    registry: ModelRegistry,
    artifact_path,
    *,
    product: str = "loan",
    feature_columns: list[str] | None = None,
    model_name: str = "Champion",
    **overrides,
) -> None:
    features = list(
        feature_columns
        if feature_columns is not None
        else (LOAN_FEATURE_COLUMNS if product == "loan" else DEPOSIT_FEATURE_COLUMNS)
    )
    record = {
        "model_name": model_name,
        "version": "v-test",
        "champion_status": "champion",
        "governance_status": "approved",
        "artifact_path": str(artifact_path),
        "feature_columns": features,
        "feature_names": features,
        "n_features": len(features),
        "product": product,
        "artifact_sha256": _sha(artifact_path) if artifact_path.exists() else "0" * 64,
    }
    record.update(overrides)
    registry.register(record)


def _register_pending(registry: ModelRegistry, artifact_path, *, product_features, name="Cand"):
    registry.register(
        {
            "model_name": name,
            "version": "v1",
            "champion_status": "challenger",
            "governance_status": "pending_human_approval",
            "artifact_path": str(artifact_path),
            "feature_columns": list(product_features),
        }
    )


def _resolve(registry: ModelRegistry, product: str = "loan"):
    agent = ModelScoringAgent(registry=registry)
    if product == "loan":
        return agent.resolve_response_fn(LOAN_FEATURE_COLUMNS, fallback=LOAN_FALLBACK, product="loan")
    return agent.resolve_response_fn(
        DEPOSIT_FEATURE_COLUMNS, fallback=DEPOSIT_FALLBACK, product="deposit"
    )


# --------------------------------------------------------------------------- persistence


def test_artifact_sha256_matches_hashlib_of_file_bytes(tmp_path):
    path = tmp_path / "m.joblib"
    save_model({"a": 1}, path)

    assert artifact_sha256(path) == _sha(path)


def test_load_verified_model_returns_object_when_hash_matches(tmp_path):
    path = tmp_path / "m.joblib"
    save_model({"a": 1}, path)

    assert load_verified_model(path, _sha(path)) == {"a": 1}


def test_load_verified_model_rejects_mismatch_before_deserialising(tmp_path, monkeypatch):
    path = tmp_path / "m.joblib"
    save_model({"a": 1}, path)
    calls: list[object] = []
    monkeypatch.setattr(joblib, "load", lambda *a, **k: calls.append(a))

    with pytest.raises(ArtifactIntegrityError, match="SHA-256 mismatch"):
        load_verified_model(path, "0" * 64)
    assert calls == []


# --------------------------------------------------------------------------- J cases


def test_j1_approved_champion_missing_artifact_raises(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, tmp_path / "nope.joblib")

    with pytest.raises(GovernanceError, match="missing"):
        _resolve(registry)


def test_j2_approved_champion_corrupt_artifact_raises(tmp_path):
    corrupt = tmp_path / "corrupt.joblib"
    corrupt.write_bytes(b"this is not a joblib pickle \x00\x01\x02")
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, corrupt)

    with pytest.raises(GovernanceError, match="could not be loaded"):
        _resolve(registry)


def test_j2b_approved_champion_loadable_dict_raises(tmp_path):
    artifact = tmp_path / "dict.joblib"
    joblib.dump({"hello": "world"}, artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact)

    with pytest.raises(GovernanceError, match="prediction interface"):
        _resolve(registry)


def test_j3_unapproved_champion_falls_back(tmp_path):
    artifact = tmp_path / "good.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact, governance_status="pending_human_approval")

    assert _resolve(registry) is LOAN_FALLBACK


def test_j4b_artifact_trained_on_different_features_raises(tmp_path):
    dataset = _loan_dataset()
    subset = [c for c in LOAN_FEATURE_COLUMNS if c != "risk_segment"]
    artifact = tmp_path / "subset.joblib"
    save_model(build_logistic_regression_model(42).fit(dataset[subset], dataset[LOAN_TARGET_COLUMN]), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact)

    with pytest.raises(GovernanceError, match="feature count"):
        _resolve(registry)


def test_j4c_numpy_trained_permuted_artifact_without_names_raises(tmp_path):
    dataset = _loan_dataset()
    permuted = list(reversed(LOAN_FEATURE_COLUMNS))
    model = build_logistic_regression_model(42).fit(
        dataset[permuted].to_numpy(), dataset[LOAN_TARGET_COLUMN]
    )
    artifact = tmp_path / "permuted.joblib"
    save_model(model, artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact)

    with pytest.raises(GovernanceError, match="feature names"):
        _resolve(registry)


def test_named_artifact_with_permuted_order_raises(tmp_path):
    dataset = _loan_dataset()
    permuted = list(reversed(LOAN_FEATURE_COLUMNS))
    artifact = tmp_path / "permuted_named.joblib"
    save_model(
        build_logistic_regression_model(42).fit(dataset[permuted], dataset[LOAN_TARGET_COLUMN]),
        artifact,
    )
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact)

    with pytest.raises(GovernanceError, match="feature order"):
        _resolve(registry)


# --------------------------------------------------------------------------- T cases


def test_t1_sha_mismatch_raises_without_deserialising(tmp_path, monkeypatch):
    artifact = tmp_path / "good.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact, artifact_sha256="f" * 64)
    calls: list[object] = []
    monkeypatch.setattr(joblib, "load", lambda *a, **k: calls.append(a))

    with pytest.raises(GovernanceError, match="SHA-256 mismatch"):
        _resolve(registry)
    assert calls == []


def test_t2_artifact_replaced_after_approval_raises(tmp_path):
    artifact = tmp_path / "good.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _register_pending(registry, artifact, product_features=LOAN_FEATURE_COLUMNS)
    registry.promote_champion("Cand", approver="validator", product="loan")
    assert isinstance(_resolve(registry), MLResponseFnAdapter)

    # Replace the approved bytes with a different (but otherwise valid) model.
    other = build_logistic_regression_model(7).fit(
        _loan_dataset(seed=99)[LOAN_FEATURE_COLUMNS], _loan_dataset(seed=99)[LOAN_TARGET_COLUMN]
    )
    save_model(other, artifact)

    with pytest.raises(GovernanceError, match="SHA-256 mismatch"):
        _resolve(registry)


def test_t3_legacy_approved_champion_without_integrity_metadata_fails_closed(tmp_path):
    artifact = tmp_path / "good.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "Legacy",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(artifact),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    with pytest.raises(GovernanceError, match="re-approv"):
        _resolve(registry)
    # Resolution must not backfill a hash into the record.
    assert "artifact_sha256" not in registry.latest_for_model("Legacy")


def test_t3_legacy_champion_can_be_explicitly_reapproved(tmp_path):
    artifact = tmp_path / "good.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "Legacy",
            "champion_status": "champion",
            "governance_status": "approved",
            "approved_by": "old-approver",
            "approved_at_utc": "2026-01-01T00:00:00+00:00",
            "artifact_path": str(artifact),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    registry.approve_model("Legacy", approver="validator", product="loan")

    record = registry.latest_for_model("Legacy")
    assert record["artifact_sha256"] == _sha(artifact)
    assert record["previous_approvals"] == [
        {"approved_by": "old-approver", "approved_at_utc": "2026-01-01T00:00:00+00:00"}
    ]
    assert isinstance(_resolve(registry), MLResponseFnAdapter)


@pytest.mark.parametrize(
    ("product", "mode"),
    [("loan", "inverted_loan"), ("deposit", "inverted_deposit")],
    ids=["t4_loan_inverted", "t5_deposit_inverted"],
)
def test_t4_t5_economically_inverted_response_raises(tmp_path, product, mode):
    features = LOAN_FEATURE_COLUMNS if product == "loan" else DEPOSIT_FEATURE_COLUMNS
    artifact = tmp_path / f"{mode}.joblib"
    save_model(StubResponseModel(features, mode), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact, product=product)

    with pytest.raises(GovernanceError, match="economically inverted"):
        _resolve(registry, product)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("nan", "non-finite"),
        ("inf", "non-finite"),
        ("out_of_range", r"outside \[0, 1\]"),
        ("bad_shape", "shape"),
    ],
    ids=["t6_nan", "t7_inf", "t8_out_of_range", "t9_bad_shape"],
)
def test_t6_to_t9_invalid_probability_output_raises(tmp_path, mode, message):
    artifact = tmp_path / f"{mode}.joblib"
    save_model(StubResponseModel(LOAN_FEATURE_COLUMNS, mode), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact)

    with pytest.raises(GovernanceError, match=message):
        _resolve(registry)


def test_flat_response_cannot_establish_direction_and_raises(tmp_path):
    artifact = tmp_path / "flat.joblib"
    save_model(StubResponseModel(LOAN_FEATURE_COLUMNS, "flat"), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact)

    with pytest.raises(GovernanceError, match="direction could not be established"):
        _resolve(registry)


def test_saturated_but_correctly_signed_response_passes(tmp_path):
    artifact = tmp_path / "saturated.joblib"
    save_model(StubResponseModel(LOAN_FEATURE_COLUMNS, "saturated_loan"), artifact)

    _, evidence = load_and_validate_artifact(artifact, LOAN_FEATURE_COLUMNS, "loan")

    means = evidence.probe["mean_probabilities"]
    steps = np.diff(means)
    # Some steps are effectively equal (saturated tails) and that is not a violation.
    assert np.any(np.abs(steps) <= MONOTONE_TOLERANCE)
    assert evidence.probe["direction"] == "non_increasing"


def test_t10_correct_approved_champions_resolve_to_adapter(tmp_path):
    loan_artifact = tmp_path / "loan.joblib"
    deposit_artifact = tmp_path / "deposit.joblib"
    save_model(_trained_loan_model(), loan_artifact)
    save_model(_trained_deposit_model(), deposit_artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _register_pending(registry, loan_artifact, product_features=LOAN_FEATURE_COLUMNS, name="Loan")
    _register_pending(
        registry, deposit_artifact, product_features=DEPOSIT_FEATURE_COLUMNS, name="Deposit"
    )
    registry.promote_champion("Loan", approver="validator", product="loan")
    registry.promote_champion("Deposit", approver="validator", product="deposit")

    loan_fn = _resolve(registry, "loan")
    deposit_fn = _resolve(registry, "deposit")

    assert isinstance(loan_fn, MLResponseFnAdapter)
    assert isinstance(deposit_fn, MLResponseFnAdapter)
    customers = generate_loan_customers(20, make_rng(1))
    probabilities = loan_fn(customers, offered_rate=0.06, competitor_rate=0.06)
    assert probabilities.shape == (20,)


def test_t10_bootstrap_ensemble_exposes_verifiable_feature_contract():
    dataset = _loan_dataset()
    model = BootstrapEnsembleResponseModel(n_estimators=2, random_state=3)
    model.fit(dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)

    assert list(model.feature_names_in_) == LOAN_FEATURE_COLUMNS
    assert model.n_features_in_ == len(LOAN_FEATURE_COLUMNS)


def test_bootstrap_ensemble_without_fitted_members_has_no_feature_contract():
    model = BootstrapEnsembleResponseModel(n_estimators=2)

    assert getattr(model, "feature_names_in_", None) is None
    assert getattr(model, "n_features_in_", None) is None


def test_t11_approval_records_verified_integrity_metadata(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _register_pending(registry, artifact, product_features=LOAN_FEATURE_COLUMNS)

    registry.promote_champion("Cand", approver="validator", product="loan")

    record = registry.latest_for_model("Cand")
    assert record["governance_status"] == "approved"
    assert record["champion_status"] == "champion"
    assert record["artifact_sha256"] == _sha(artifact)
    assert record["feature_names"] == LOAN_FEATURE_COLUMNS
    assert record["n_features"] == len(LOAN_FEATURE_COLUMNS)
    assert record["product"] == "loan"
    assert record["prediction_interface"] == "predict_proba"
    validation = record["integrity_validation"]
    assert validation["status"] == "passed"
    assert validation["schema_version"] == champion_integrity.INTEGRITY_SCHEMA_VERSION
    assert validation["probe"]["direction"] == "non_increasing"
    assert "validated_at_utc" in validation


@pytest.mark.parametrize(
    ("artifact_kind", "product", "message"),
    [
        ("dict", "loan", "prediction interface"),
        ("inverted_loan", "loan", "economically inverted"),
        ("missing", "loan", "missing"),
        ("good", None, "product"),
        ("good", "mortgage", "product"),
    ],
)
def test_t11_approval_refuses_unusable_artifact_and_leaves_record_unchanged(
    tmp_path, artifact_kind, product, message
):
    artifact = tmp_path / "artifact.joblib"
    if artifact_kind == "dict":
        joblib.dump({"hello": "world"}, artifact)
    elif artifact_kind == "inverted_loan":
        save_model(StubResponseModel(LOAN_FEATURE_COLUMNS, "inverted_loan"), artifact)
    elif artifact_kind == "good":
        save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _register_pending(registry, artifact, product_features=LOAN_FEATURE_COLUMNS)
    before = registry.registry_path.read_text(encoding="utf-8")

    with pytest.raises(ChampionValidationError, match=message):
        registry.promote_champion("Cand", approver="validator", product=product)

    assert registry.registry_path.read_text(encoding="utf-8") == before


def test_approval_refuses_record_without_artifact_path(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "NoArtifact", "feature_columns": LOAN_FEATURE_COLUMNS})

    with pytest.raises(ChampionValidationError, match="artifact_path"):
        registry.approve_model("NoArtifact", approver="validator", product="loan")


def test_approval_refuses_record_without_feature_contract(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "NoFeatures", "artifact_path": str(artifact)})

    with pytest.raises(ChampionValidationError, match="feature_columns"):
        registry.approve_model("NoFeatures", approver="validator", product="loan")


def test_approval_refuses_product_conflicting_with_recorded_product(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "Cand",
            "artifact_path": str(artifact),
            "feature_columns": LOAN_FEATURE_COLUMNS,
            "product": "loan",
        }
    )

    with pytest.raises(ChampionValidationError, match="product"):
        registry.approve_model("Cand", approver="validator", product="deposit")


def test_approval_uses_recorded_product_when_not_supplied(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "Cand",
            "artifact_path": str(artifact),
            "feature_columns": LOAN_FEATURE_COLUMNS,
            "product": "loan",
        }
    )

    registry.approve_model("Cand", approver="validator")

    assert registry.latest_for_model("Cand")["artifact_sha256"] == _sha(artifact)


def test_reapproval_refuses_artifact_changed_since_recorded_hash(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _register_pending(registry, artifact, product_features=LOAN_FEATURE_COLUMNS)
    registry.approve_model("Cand", approver="validator", product="loan")
    save_model(_trained_deposit_model(), artifact)  # replace bytes

    with pytest.raises(ChampionValidationError, match="changed since"):
        registry.approve_model("Cand", approver="validator", product="loan")


def test_t12_incompatible_champion_falls_back(tmp_path):
    artifact = tmp_path / "deposit.joblib"
    save_model(_trained_deposit_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact, product="deposit")

    # The only champion has the deposit contract, so no loan champion is compatible.
    assert _resolve(registry, "loan") is LOAN_FALLBACK


def test_t15_loan_and_deposit_contracts_are_validated_independently(tmp_path):
    loan_artifact = tmp_path / "loan.joblib"
    deposit_artifact = tmp_path / "deposit.joblib"
    save_model(_trained_loan_model(), loan_artifact)
    save_model(StubResponseModel(DEPOSIT_FEATURE_COLUMNS, "inverted_deposit"), deposit_artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, loan_artifact, product="loan", model_name="Loan")
    _force_approved(registry, deposit_artifact, product="deposit", model_name="Deposit")

    assert isinstance(_resolve(registry, "loan"), MLResponseFnAdapter)
    with pytest.raises(GovernanceError, match="economically inverted"):
        _resolve(registry, "deposit")


def test_t15_record_product_must_match_requested_product(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    # Loan feature contract, but the verified metadata says deposit.
    _force_approved(registry, artifact, product="deposit", feature_columns=LOAN_FEATURE_COLUMNS)

    with pytest.raises(GovernanceError, match="product"):
        _resolve(registry, "loan")


def test_resolution_without_product_argument_uses_recorded_product(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact, product="loan")

    fn = ModelScoringAgent(registry=registry).resolve_response_fn(
        LOAN_FEATURE_COLUMNS, fallback=LOAN_FALLBACK
    )

    assert isinstance(fn, MLResponseFnAdapter)


@pytest.mark.parametrize(
    "overrides",
    [
        {"feature_names": list(reversed(LOAN_FEATURE_COLUMNS))},
        {"n_features": 9},
        {"product": "mortgage", "feature_columns": LOAN_FEATURE_COLUMNS},
        {"artifact_sha256": "not-a-hash"},
        {"feature_names": 10},
    ],
    ids=["feature_names", "n_features", "product", "sha_format", "feature_names_type"],
)
def test_resolution_rejects_inconsistent_recorded_metadata(tmp_path, overrides):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, artifact, **overrides)

    with pytest.raises(GovernanceError):
        _resolve(registry)


def test_governance_error_message_does_not_echo_artifact_bytes(tmp_path):
    secret_marker = b"TOP-SECRET-PAYLOAD"
    corrupt = tmp_path / "corrupt.joblib"
    corrupt.write_bytes(secret_marker * 10)
    registry = ModelRegistry(tmp_path / "registry.json")
    _force_approved(registry, corrupt)

    with pytest.raises(GovernanceError) as info:
        _resolve(registry)
    assert "TOP-SECRET" not in str(info.value)


def test_validation_evidence_round_trips_to_json(tmp_path):
    artifact = tmp_path / "loan.joblib"
    save_model(_trained_loan_model(), artifact)

    _, evidence = load_and_validate_artifact(artifact, LOAN_FEATURE_COLUMNS, "loan")

    payload = json.loads(json.dumps(evidence.to_dict()))
    assert payload["artifact_sha256"] == _sha(artifact)
    assert payload["feature_names"] == LOAN_FEATURE_COLUMNS
    assert payload["product"] == "loan"
    assert payload["probe"]["tolerance"] == MONOTONE_TOLERANCE


# --------------------------------------------------------------------------- unit branches


@pytest.mark.parametrize(
    ("contract", "message"),
    [
        ([], "non-empty"),
        ("offered_rate", "non-empty"),
        (["offered_rate", ""], "non-empty strings"),
        (["offered_rate", "offered_rate"], "duplicates"),
    ],
)
def test_validate_feature_contract_rejects_unusable_contracts(contract, message):
    with pytest.raises(ChampionValidationError, match=message):
        champion_integrity.validate_feature_contract(contract)


def test_validate_prediction_interface_rejects_a_class_object():
    with pytest.raises(ChampionValidationError, match="is a class"):
        champion_integrity.validate_prediction_interface(StubResponseModel)


def test_validate_prediction_interface_prefers_response_model_protocol():
    model = BootstrapEnsembleResponseModel(n_estimators=1)

    assert champion_integrity.validate_prediction_interface(model) == "predict_probability"


class _NamesOnly:
    def __init__(self, names):
        self.feature_names_in_ = np.asarray(names, dtype=object)


def test_validate_artifact_features_rejects_name_count_mismatch_without_n_features():
    with pytest.raises(ChampionValidationError, match="feature count"):
        champion_integrity.validate_artifact_features(
            _NamesOnly(LOAN_FEATURE_COLUMNS[:-1]), LOAN_FEATURE_COLUMNS
        )


def test_validate_artifact_features_reports_unexpected_and_missing_names():
    names = [*LOAN_FEATURE_COLUMNS[:-1], "postcode"]

    with pytest.raises(ChampionValidationError, match="feature name mismatch") as info:
        champion_integrity.validate_artifact_features(_NamesOnly(names), LOAN_FEATURE_COLUMNS)
    assert "postcode" in str(info.value)
    assert "risk_segment" in str(info.value)


class _ObjectOutput(StubResponseModel):
    """ResponseModel-protocol stub whose output is returned to the adapter unconverted."""

    def predict_probability(self, frame, feature_columns):
        return np.array(["high"] * len(frame), dtype=object)


class _Raises(StubResponseModel):
    def predict_proba(self, frame):
        raise RuntimeError("boom")


def test_probe_rejects_non_numeric_predictions():
    with pytest.raises(ChampionValidationError, match="non-numeric"):
        champion_integrity.probe_response(
            _ObjectOutput(LOAN_FEATURE_COLUMNS, "flat"), LOAN_FEATURE_COLUMNS, "loan"
        )


def test_probe_wraps_scoring_exceptions_without_echoing_their_text():
    with pytest.raises(ChampionValidationError, match="RuntimeError") as info:
        champion_integrity.probe_response(
            _Raises(LOAN_FEATURE_COLUMNS, "flat"), LOAN_FEATURE_COLUMNS, "loan"
        )
    assert "boom" not in str(info.value)


def test_probe_is_deterministic_and_does_not_touch_global_random_state():
    model = StubResponseModel(LOAN_FEATURE_COLUMNS, "sane_loan")
    np.random.seed(5)
    before = np.random.random()
    np.random.seed(5)

    first = champion_integrity.probe_response(model, LOAN_FEATURE_COLUMNS, "loan")
    second = champion_integrity.probe_response(model, LOAN_FEATURE_COLUMNS, "loan")

    assert first == second
    assert np.random.random() == before


def test_load_and_validate_rejects_unknown_product(tmp_path):
    with pytest.raises(ChampionValidationError, match="product"):
        load_and_validate_artifact(tmp_path / "x.joblib", LOAN_FEATURE_COLUMNS, "mortgage")

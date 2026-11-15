"""GOV-004: decision-record config hash is the effective config, not the source file.

``config_hash``                -> canonical hash of the validated config actually priced.
``source_config_file_sha256``  -> SHA-256 of the caller's original YAML bytes.
They answer different questions and must never be conflated.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from balance_pricing_engine.application.contracts import PricingScenarioRequest
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.competitor_rates.apply import apply_competitor_rates_to_config
from balance_pricing_engine.competitor_rates.store import load_rate_run
from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.config_identity import effective_config_hash
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

SUMMARY_KEYS = {
    "best_loan_rate",
    "best_deposit_rate",
    "objective_value",
    "expected_loan_volume",
    "expected_deposit_volume",
    "funding_shortfall",
    "excess_deposit_income",
    "boundary_warning_flag",
    "initial_boundary_warning_flag",
    "expansion_rounds",
    "expansion_capped",
    "policy_band_breached",
    "roe_constraint_relaxed",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _small(config: EngineConfig) -> EngineConfig:
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 500, "n_deposit_customers": 500}
    )
    return config.model_copy(update={"simulation": simulation})


def _override(config: EngineConfig, **response_updates: float) -> EngineConfig:
    response = config.response.model_copy(update=response_updates)
    return config.model_copy(update={"response": response})


@pytest.fixture()
def source_yaml(tmp_path) -> Path:
    path = tmp_path / "caller_config.yaml"
    shutil.copyfile("configs/base.yaml", path)
    return path


@pytest.fixture()
def base_config() -> EngineConfig:
    return _small(load_config("configs/base.yaml"))


def _run(config, source_yaml, tmp_path, **kwargs):
    return PricingService().run_scenario(
        PricingScenarioRequest(
            config=config,
            config_path=source_yaml,
            ledger_base_dir=tmp_path / "runs",
            max_boundary_expansions=0,
            **kwargs,
        )
    )


def _competitor_config(tmp_path) -> EngineConfig:
    run = load_rate_run("tests/fixtures/competitor_rates/latest.json")
    source_file = tmp_path / "latest.json"
    source_file.write_text(run.model_dump_json(), encoding="utf-8")
    raw = _small(load_config("configs/base.yaml")).model_dump()
    raw["competitor_rates"] = {
        **raw["competitor_rates"],
        "enabled": True,
        "source_file": str(source_file),
        "max_staleness_hours": 10_000,
        "strict": True,
    }
    return EngineConfig.model_validate(raw)


# --- In-memory overrides (the audit failure) ------------------------------------------


@pytest.mark.parametrize(
    "updates",
    [{"loan_rate_elasticity": -60.0}, {"deposit_rate_elasticity": 60.0}],
    ids=["loan_elasticity", "deposit_elasticity"],
)
def test_in_memory_override_hashes_effective_config_not_source_file(
    updates, base_config, source_yaml, tmp_path
):
    """T04/T05/T11 + A-D of the audit repro."""
    source_before = source_yaml.read_bytes()
    overridden = _override(base_config, **updates)

    base_decision = _run(base_config, source_yaml, tmp_path)
    decision = _run(overridden, source_yaml, tmp_path)
    record = decision.decision_record

    assert record.config_hash == effective_config_hash(overridden)  # C
    assert record.config_hash != effective_config_hash(base_config)  # A
    assert record.config_hash != base_decision.decision_record.config_hash  # A
    assert record.config_hash != _sha(source_yaml)  # never the file hash
    assert record.source_config_file_sha256 == _sha(source_yaml)  # B
    assert base_decision.decision_record.source_config_file_sha256 == _sha(source_yaml)  # B
    assert source_yaml.read_bytes() == source_before  # D


def test_equivalent_overrides_produce_same_decision_hash(base_config, source_yaml, tmp_path):
    """E: different routes to identical effective config -> same config_hash."""
    first = _override(base_config, loan_rate_elasticity=-60.0)
    second = EngineConfig.model_validate(first.model_dump())
    other_source = tmp_path / "other_name.yaml"
    other_source.write_text("# different bytes, same economics\n" + source_yaml.read_text())

    one = _run(first, source_yaml, tmp_path).decision_record
    two = _run(second, other_source, tmp_path).decision_record

    assert one.config_hash == two.config_hash
    assert one.source_config_file_sha256 != two.source_config_file_sha256


def test_config_without_source_path_hashes_effective_config(base_config):
    """T08"""
    decision = PricingService().run_scenario(
        PricingScenarioRequest(config=base_config, max_boundary_expansions=0)
    )
    assert decision.decision_record.config_hash == effective_config_hash(base_config)
    assert decision.decision_record.source_config_file_sha256 is None


def test_pricing_values_unchanged_by_hashing(base_config, source_yaml, tmp_path):
    """T16: the decision still equals the plain optimiser result."""
    overridden = _override(base_config, loan_rate_elasticity=-60.0)
    expected = run_baseline_diagnostics(overridden, max_boundary_expansions=0).best_result

    decision = _run(overridden, source_yaml, tmp_path)

    assert decision.best_loan_rate == expected.loan_rate
    assert decision.best_deposit_rate == expected.deposit_rate
    assert decision.objective_value == pytest.approx(expected.total_objective)


# --- Competitor-rate resolution ---------------------------------------------------------


def test_resolved_competitor_rates_drive_hash_and_source_stays_independent(
    tmp_path, source_yaml
):
    """T09/T10 + E/F: hash is of the post-resolution config (both loan and deposit rates)."""
    config = _competitor_config(tmp_path)
    resolved = apply_competitor_rates_to_config(config)
    assert resolved.market.competitor_loan_rate != config.market.competitor_loan_rate
    assert resolved.market.competitor_deposit_rate != config.market.competitor_deposit_rate

    decision = _run(config, source_yaml, tmp_path)
    record = decision.decision_record

    expected = run_baseline_diagnostics(resolved, max_boundary_expansions=0).best_result
    assert decision.best_loan_rate == expected.loan_rate  # priced on resolved rates
    assert record.config_hash == effective_config_hash(resolved)
    assert record.config_hash != effective_config_hash(config)  # not pre-resolution
    loan_only = resolved.model_copy(
        update={
            "market": resolved.market.model_copy(
                update={"competitor_deposit_rate": config.market.competitor_deposit_rate}
            )
        }
    )
    deposit_only = resolved.model_copy(
        update={
            "market": resolved.market.model_copy(
                update={"competitor_loan_rate": config.market.competitor_loan_rate}
            )
        }
    )
    assert record.config_hash != effective_config_hash(loan_only)  # deposit rate matters
    assert record.config_hash != effective_config_hash(deposit_only)  # loan rate matters
    # Source provenance is the caller's file, never the temporary effective overlay.
    assert record.source_config_file_sha256 == _sha(source_yaml)


# --- Ledger -----------------------------------------------------------------------------


def test_persisted_ledger_carries_verifiable_effective_config(
    base_config, source_yaml, tmp_path
):
    """T12/T13/T14: the recorded hash can be re-derived from effective_config.json."""
    overridden = _override(base_config, loan_rate_elasticity=-60.0)
    decision = _run(
        overridden, source_yaml, tmp_path, persist_ledger=True, scenario_name="lineage"
    )
    run_dir = decision.run_dir
    record = json.loads((run_dir / "decision_record.json").read_text(encoding="utf-8"))
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    effective_file = run_dir / "effective_config.json"

    assert effective_file.is_file()
    assert _sha(effective_file) == record["config_hash"]  # verifiable
    assert metadata["config_hash"] == record["config_hash"]  # one definition, no conflict
    assert record["source_config_file_sha256"] == _sha(source_yaml)
    assert metadata["source_config_file_sha256"] == _sha(source_yaml)
    assert record["config_hash_basis"] == "effective_config_canonical_json_sha256"
    priced = EngineConfig.model_validate_json(effective_file.read_text(encoding="utf-8"))
    assert priced.response.loan_rate_elasticity == -60.0
    assert effective_config_hash(priced) == record["config_hash"]
    # No absolute server path or file content leaks into the new provenance fields.
    assert str(tmp_path) not in json.dumps(
        {k: record[k] for k in ("config_hash", "source_config_file_sha256", "config_hash_basis")}
    )


def test_existing_summary_and_grid_contracts_unchanged(base_config, source_yaml, tmp_path):
    """T14/T15"""
    from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS

    decision = _run(base_config, source_yaml, tmp_path, persist_ledger=True)
    summary = json.loads((decision.run_dir / "summary.json").read_text(encoding="utf-8"))
    header = (decision.run_dir / "grid_results.csv").read_text(encoding="utf-8").splitlines()[0]

    assert SUMMARY_KEYS <= set(summary)
    assert not any("config" in key for key in summary)
    assert header.split(",") == GRID_RESULT_COLUMNS


def test_competitor_ledger_effective_config_json_matches_resolved_rates(tmp_path, source_yaml):
    config = _competitor_config(tmp_path)
    resolved = apply_competitor_rates_to_config(config)
    decision = _run(config, source_yaml, tmp_path, persist_ledger=True)

    effective_file = decision.run_dir / "effective_config.json"
    assert _sha(effective_file) == decision.decision_record.config_hash
    stored = EngineConfig.model_validate_json(effective_file.read_text(encoding="utf-8"))
    assert stored.market.competitor_loan_rate == pytest.approx(
        resolved.market.competitor_loan_rate
    )
    metadata = json.loads((decision.run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_config_file_sha256"] == _sha(source_yaml)


# --- Legacy / direct callers --------------------------------------------------------------


def test_legacy_decision_record_json_without_new_fields_is_still_readable():
    """T19: new fields are additive; old JSON keeps its shape and defaults apply."""
    from dataclasses import fields

    from balance_pricing_engine.governance.decision_record import DecisionRecord

    legacy_keys = {f.name for f in fields(DecisionRecord)} - {
        "source_config_file_sha256",
        "config_hash_basis",
    }
    new_fields = {f.name: f for f in fields(DecisionRecord)}
    assert new_fields["source_config_file_sha256"].default is None
    assert new_fields["config_hash_basis"].default is not None
    assert "config_hash" in legacy_keys


def test_direct_write_run_ledger_records_effective_hash_and_source_hash(
    base_config, source_yaml, tmp_path
):
    """T18: direct write_run_ledger callers (CLI/examples) get the same semantics."""
    from balance_pricing_engine.run_ledger import write_run_ledger

    overridden = _override(base_config, loan_rate_elasticity=-60.0)
    diagnostics = run_baseline_diagnostics(overridden, max_boundary_expansions=0)
    run_dir = write_run_ledger(
        scenario_name="direct",
        config=overridden,
        config_path=source_yaml,
        diagnostics=diagnostics,
        base_dir=tmp_path / "runs",
    )
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))

    assert metadata["config_hash"] == effective_config_hash(overridden)
    assert metadata["source_config_file_sha256"] == _sha(source_yaml)
    assert _sha(run_dir / "effective_config.json") == metadata["config_hash"]
    assert (run_dir / "config_snapshot.yaml").read_bytes() == source_yaml.read_bytes()

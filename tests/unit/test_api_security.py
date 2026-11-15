"""Offline regressions for the API's network and untrusted-input boundaries."""

import importlib
import hashlib
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from balance_pricing_engine.api import app
from balance_pricing_engine.api import server

api_module = importlib.import_module("balance_pricing_engine.api.app")
CONFIG_YAML = Path("configs/base.yaml").read_text(encoding="utf-8")
ROUTES = ("/run-scenario", "/v1/pricing/scenarios")


@pytest.fixture
def launcher(monkeypatch):
    for name in (
        "PRICING_ENGINE_API_HOST",
        "PRICING_ENGINE_API_KEYS",
        "PRICING_ENGINE_API_PORT",
        "PRICING_ENGINE_TLS_CERTFILE",
        "PRICING_ENGINE_TLS_KEYFILE",
        "PRICING_ENGINE_API_LIMIT_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)
    calls = []
    # The dev extra includes FastAPI, while uvicorn is an optional api extra.
    # Intercept the launch boundary without importing or running a real server.
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs))),
    )
    return calls


def test_default_launcher_is_loopback_and_bounds_concurrency(launcher):
    server.main()
    assert launcher[0][1]["host"] == "127.0.0.1"
    assert launcher[0][1]["limit_concurrency"] == 4


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "example.invalid"])
def test_external_bind_without_auth_fails_before_uvicorn(monkeypatch, launcher, host):
    monkeypatch.setenv("PRICING_ENGINE_API_HOST", host)
    with pytest.raises(ValueError, match="authentication"):
        server.main()
    assert launcher == []


def test_external_bind_with_rotation_keys_passes_validation(monkeypatch, launcher):
    monkeypatch.setenv("PRICING_ENGINE_API_HOST", "0.0.0.0")
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "old-key, new-key")
    server.main()
    assert launcher[0][1]["host"] == "0.0.0.0"


@pytest.mark.parametrize("keys", ["", " , ", "key,", ",key", "key,,other", "key\nsecret", "kéy"])
def test_malformed_keys_fail_startup_without_echoing_secret(monkeypatch, launcher, keys):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", keys)
    with pytest.raises(ValueError, match="PRICING_ENGINE_API_KEYS") as error:
        server.main()
    assert "secret" not in str(error.value)
    assert launcher == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("keys", ["key,", "key,,other", "key\nsecret", "kéy"])
def test_malformed_keys_fail_closed_on_requests(monkeypatch, route, keys):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", keys)
    response = TestClient(app).post(
        route, json={"config_yaml": CONFIG_YAML}, headers={"X-API-Key": "key"}
    )
    assert response.status_code == 500
    assert response.json()["error_code"] == "AUTH_CONFIG_INVALID"
    assert "secret" not in response.text


@pytest.mark.parametrize("route", ROUTES)
def test_both_routes_require_valid_key_and_preserve_rotation(monkeypatch, route):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "old-key,new-key")
    client = TestClient(app)
    for headers in ({}, {"X-API-Key": "invalid"}):
        assert (
            client.post(route, json={"config_yaml": CONFIG_YAML}, headers=headers).status_code
            == 401
        )
    for key in ("old-key", "new-key"):
        response = client.post(route, json={"config_yaml": CONFIG_YAML}, headers={"X-API-Key": key})
        assert response.status_code == 200


@pytest.mark.parametrize(
    "name,value",
    [
        ("PRICING_ENGINE_API_PORT", "internal-secret-path"),
        ("PRICING_ENGINE_API_HOST", ""),
        ("PRICING_ENGINE_API_LIMIT_CONCURRENCY", "0"),
    ],
)
def test_bad_launcher_options_fail_safely(monkeypatch, launcher, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        server.main()
    assert "internal-secret-path" not in str(error.value)
    assert launcher == []


def test_tls_configuration_error_does_not_echo_internal_paths():
    with pytest.raises(ValueError) as error:
        server.resolve_tls_kwargs({"PRICING_ENGINE_TLS_CERTFILE": "/private/internal/cert"})
    assert "/private/internal" not in str(error.value)


@pytest.fixture
def forbid_execution(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Request reached expensive execution")

    monkeypatch.setattr(api_module, "run_baseline_diagnostics", forbidden)
    monkeypatch.setattr(api_module.PricingService, "run_scenario", forbidden)
    return calls


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "mutation",
    [
        "yaml_bytes",
        "loan_customers",
        "deposit_customers",
        "grid_dimension",
        "grid_combined",
        "expanded_grid",
        "sensitivity",
        "tiny_step",
        "absolute_grid",
    ],
)
def test_rejects_excessive_work_before_execution(route, mutation, forbid_execution):
    raw = yaml.safe_load(CONFIG_YAML)
    include_sensitivity = False
    if mutation == "loan_customers":
        raw["simulation"]["n_loan_customers"] = 25_001
    elif mutation == "deposit_customers":
        raw["simulation"]["n_deposit_customers"] = 25_001
    elif mutation in {"grid_dimension", "tiny_step"}:
        raw["pricing_strategy"]["home_loan_spread_bps"]["step"] = (
            0.0001 if mutation == "grid_dimension" else 1e-320
        )
    elif mutation == "absolute_grid":
        del raw["pricing_strategy"]
        raw["products"] = {
            "loan_rate_grid": {"start": 0.01, "stop": 0.10, "step": 1e-9},
            "deposit_rate_grid": {"start": 0.01, "stop": 0.05, "step": 0.01},
        }
    elif mutation in {"grid_combined", "expanded_grid", "sensitivity"}:
        raw["pricing_strategy"] = {
            "home_loan_spread_bps": {"min": -50, "max": 50, "step": 1},
            "term_deposit_spread_bps": {
                "min": -50,
                "max": 50,
                "step": 0.5
                if mutation == "grid_combined"
                else (2 if mutation == "expanded_grid" else 10),
            },
        }
        include_sensitivity = mutation == "sensitivity"
    config_yaml = yaml.safe_dump(raw)
    if mutation == "yaml_bytes":
        config_yaml += "#" + "x" * 65_536
    response = TestClient(app).post(
        route,
        json={
            "config_yaml": config_yaml,
            "include_sensitivity": include_sensitivity,
        },
    )
    assert response.status_code == 422, response.text
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "payload",
    [
        "a: &loop [*loop]",
        "a: &base [1, 2]\nb: [*base, *base]",
        "a: " + "[" * 40 + "0" + "]" * 40,
        "a: " + "[" * 40,
        "random_seed: 42\nrandom_seed: 43\n",
        "a: [" + ",".join("0" for _ in range(5000)) + "]",
    ],
)
def test_complex_or_ambiguous_yaml_is_rejected(route, payload, forbid_execution):
    response = TestClient(app).post(route, json={"config_yaml": payload})
    assert response.status_code == 422
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
def test_streamed_body_limit_cannot_be_bypassed_without_content_length(route, forbid_execution):
    chunks = (b" " * 8192 for _ in range(70))
    response = TestClient(app).post(
        route, content=chunks, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "size" in response.text.lower() or "limit" in response.text.lower()
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
def test_operator_can_lower_adaptive_budget(monkeypatch, route, forbid_execution):
    monkeypatch.setenv("PRICING_ENGINE_API_MAX_ADAPTIVE_EVALUATIONS", "6000")
    response = TestClient(app).post(route, json={"config_yaml": CONFIG_YAML})
    assert response.status_code == 422
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("enabled", [True, False])
def test_client_competitor_paths_never_reach_execution(route, enabled, forbid_execution):
    raw = yaml.safe_load(CONFIG_YAML)
    raw["competitor_rates"].update(enabled=enabled, source_file="/private/server/secret.json")
    response = TestClient(app).post(route, json={"config_yaml": yaml.safe_dump(raw)})
    assert response.status_code == 422
    assert "/private/server" not in response.text
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
def test_invalid_yaml_errors_never_echo_paths_or_secret_values(route):
    response = TestClient(app).post(
        route,
        json={
            "config_yaml": "simulation: /private/server/synthetic-secret\n",
        },
    )
    assert response.status_code == 422
    assert "/private/server" not in response.text
    assert "synthetic-secret" not in response.text


@pytest.mark.parametrize(
    "exception,status",
    [
        (FileNotFoundError("/private/server/file.json"), 422),
        (api_module.GovernanceError("/private/server/champion.joblib"), 503),
        (NotImplementedError("/private/server/unsupported"), 422),
    ],
)
def test_service_error_responses_never_echo_internal_paths(monkeypatch, exception, status):
    def fail(self, request):
        raise exception

    monkeypatch.setattr(api_module.PricingService, "run_scenario", fail)
    response = TestClient(app).post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})
    assert response.status_code == status
    assert "/private/server" not in response.text


def test_temp_config_exclusive_private_and_removed_on_execution_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))
    observed = []

    def fail(self, request):
        path = request.config_path
        observed.append(path)
        assert path.read_text() == CONFIG_YAML
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.parent == tmp_path
        raise RuntimeError("execution failed")

    monkeypatch.setattr(api_module.PricingService, "run_scenario", fail)
    response = TestClient(app).post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})
    assert response.status_code == 500
    assert observed and not observed[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_concurrent_requests_use_distinct_temp_files_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))
    barrier = Barrier(2)
    observed = []
    original = api_module._pricing_service.run_scenario

    def run(self, request):
        observed.append(request.config_path)
        barrier.wait(timeout=10)
        assert request.config_path.read_text() == CONFIG_YAML
        return original(request)

    monkeypatch.setattr(api_module.PricingService, "run_scenario", run)

    def post(_):
        return TestClient(app).post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert len(set(observed)) == 2
    assert all(path.parent == tmp_path and not path.exists() for path in observed)
    assert list(tmp_path.iterdir()) == []


def test_temp_config_is_removed_on_write_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))
    original = os.fdopen

    def fail(*args, **kwargs):
        handle = original(*args, **kwargs)
        handle.close()
        raise OSError("synthetic write failure")

    monkeypatch.setattr(os, "fdopen", fail)
    with pytest.raises(OSError):
        api_module._materialize_config_yaml(CONFIG_YAML)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", ROUTES)
def test_invalid_yaml_scalar_conversion_is_safe_422(route, forbid_execution):
    response = TestClient(app).post(route, json={"config_yaml": "unused: 2026-99-99\n"})
    assert response.status_code == 422
    assert forbid_execution == []


def test_ledger_records_effective_hash_original_yaml_hash_and_safe_relative_reference(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = TestClient(app).post(
        "/v1/pricing/scenarios",
        json={
            "config_yaml": CONFIG_YAML,
            "persist_ledger": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    run_dir = Path(payload["run_dir"])
    assert not run_dir.is_absolute()
    assert (run_dir / "config_snapshot.yaml").read_text() == CONFIG_YAML
    import json

    record = json.loads((run_dir / "decision_record.json").read_text())
    # GOV-004: config_hash is the canonical hash of the effective config; the original
    # request YAML bytes are recorded separately as source_config_file_sha256.
    from balance_pricing_engine.config_identity import effective_config_hash
    from balance_pricing_engine.api.limits import load_api_config

    source_sha = hashlib.sha256(CONFIG_YAML.encode()).hexdigest()
    assert record["source_config_file_sha256"] == source_sha
    assert record["config_hash"] == effective_config_hash(load_api_config(CONFIG_YAML))
    assert record["config_hash"] != source_sha
    effective_file = run_dir / "effective_config.json"
    assert hashlib.sha256(effective_file.read_bytes()).hexdigest() == record["config_hash"]
    assert str(tmp_path) not in response.text
    assert str(tmp_path) not in (run_dir / "decision_record.json").read_text()


def test_trusted_local_config_still_accepts_custom_competitor_file(tmp_path):
    from balance_pricing_engine.config import load_config
    from balance_pricing_engine.competitor_rates.apply import apply_competitor_rates_to_config

    raw = yaml.safe_load(CONFIG_YAML)
    raw["competitor_rates"]["source_file"] = str(tmp_path / "trusted-local.json")
    local_config = tmp_path / "local.yaml"
    local_config.write_text(yaml.safe_dump(raw))
    config = load_config(local_config)
    assert config.competitor_rates.source_file == str(tmp_path / "trusted-local.json")
    assert apply_competitor_rates_to_config(config) is config


@pytest.mark.parametrize("failure", [False, True])
def test_effective_competitor_temp_config_is_private_and_cleaned(tmp_path, monkeypatch, failure):
    from balance_pricing_engine.application import pricing_service
    from balance_pricing_engine.application.contracts import PricingScenarioRequest
    from balance_pricing_engine.config import EngineConfig

    raw = yaml.safe_load(CONFIG_YAML)
    raw["competitor_rates"]["enabled"] = True
    config = EngineConfig.model_validate(raw)
    monkeypatch.setattr(pricing_service.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(pricing_service, "apply_competitor_rates_to_config", lambda config: config)
    observed = []

    def finish(self, request, effective_path):
        observed.append(effective_path)
        assert effective_path.parent == tmp_path
        assert stat.S_IMODE(effective_path.stat().st_mode) == 0o600
        assert yaml.safe_load(effective_path.read_text()) == config.model_dump(mode="json")
        if failure:
            raise RuntimeError("synthetic failure")
        return "completed"

    monkeypatch.setattr(pricing_service.PricingService, "_run_scenario", finish)
    request = PricingScenarioRequest(config=config, config_path=tmp_path / "input.yaml")
    service = pricing_service.PricingService()
    if failure:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            service.run_scenario(request)
    else:
        assert service.run_scenario(request) == "completed"
    assert observed and not observed[0].exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "setting",
    [
        ("PRICING_ENGINE_API_MAX_YAML_BYTES", "0"),
        ("PRICING_ENGINE_API_MAX_GRID_SIZE", "private-invalid-value"),
        ("PRICING_ENGINE_API_MAX_YAML_DEPTH", "65"),
    ],
)
def test_malformed_operator_limits_fail_closed_safely(
    monkeypatch, route, setting, forbid_execution
):
    monkeypatch.setenv(*setting)
    response = TestClient(app).post(route, json={"config_yaml": CONFIG_YAML})
    assert response.status_code == 500
    assert "private-invalid-value" not in response.text
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
def test_database_path_override_is_rejected(route, forbid_execution):
    raw = yaml.safe_load(CONFIG_YAML)
    raw["pricing_intelligence"] = {"database_path": "/private/server/database.db"}
    response = TestClient(app).post(route, json={"config_yaml": yaml.safe_dump(raw)})
    assert response.status_code == 422
    assert "/private/server" not in response.text
    assert forbid_execution == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("product", ["home_loan_spread_bps", "term_deposit_spread_bps"])
def test_bps_conversion_cannot_hide_large_initial_allocations(route, product, forbid_execution):
    raw = yaml.safe_load(CONFIG_YAML)
    raw["pricing_strategy"][product] = {
        "min": 170.0,
        "max": 170.00000000000003,
        "step": 2.8421709430404006e-21,
    }
    response = TestClient(app).post(route, json={"config_yaml": yaml.safe_dump(raw)})
    assert response.status_code == 422
    assert forbid_execution == []


@pytest.mark.parametrize("step", [0.0005, 0.000025, 0.0000015, 0.0000001])
def test_expansion_budget_bounds_rounded_grids_in_both_directions(step):
    from balance_pricing_engine.api.limits import APILimits, _dimension_counts
    from balance_pricing_engine.diagnostics import _expand_grid_for_selected_rate
    from balance_pricing_engine.grid_utils import build_inclusive_numeric_grid

    start = 0.0499035
    stop = start + step * 20
    counts = _dimension_counts(start, stop, step, APILimits())
    grids = [build_inclusive_numeric_grid(start, stop, step)]
    for round_ in range(3):
        assert max(len(grid) for grid in grids) <= counts[round_]
        grids = [
            _expand_grid_for_selected_rate(grid, edge, span_multiplier=1.0)[0]
            for grid in grids
            for edge in (grid[0], grid[-1])
        ]


def test_effective_temp_config_cleanup_on_serialisation_failure(tmp_path, monkeypatch):
    from balance_pricing_engine.application import pricing_service
    from balance_pricing_engine.config import EngineConfig

    raw = yaml.safe_load(CONFIG_YAML)
    raw["competitor_rates"]["enabled"] = True
    config = EngineConfig.model_validate(raw)
    monkeypatch.setattr(pricing_service.tempfile, "tempdir", str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("synthetic write error")

    monkeypatch.setattr(pricing_service.yaml, "safe_dump", fail)
    with pytest.raises(OSError, match="synthetic write error"):
        pricing_service._materialize_effective_config(config, "input.yaml")
    assert list(tmp_path.iterdir()) == []
